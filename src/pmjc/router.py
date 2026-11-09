"""Model router: picks the first usable provider from the user's ordered list.

Order and settings come from settings.yaml (dashboard -> Routing). A provider is skipped when it is disabled,
lacks a capability the job needs, is cloud while the job is private, is over its monthly budget, is cooling
down after a failure, or is unhealthy and can't be started.

Local OpenAI-compatible hosts (LM Studio, Ollama, ...) are started on demand with their `start` command
(argv, no shell; only editable in settings.yaml, never through the API), and LM Studio models are loaded
with `lms load`. A GPU guard skips local hosts while the GPU is busy (e.g. a game is running).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import csv
import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from . import config, creds, db
from .config import ProviderConfig

log = logging.getLogger("pmjc.router")
_cooldown: dict[str, float] = {}
COOLDOWN_S = 300


@dataclass
class Need:
    capabilities: set[str] = field(default_factory=lambda: {"tools"})
    private: bool = False  # local providers only; never fall back to the cloud
    min_context: int = 0
    order: list[str] | None = None  # job-specific provider order (ids)
    interactive: bool = False  # a person is waiting: never block on starting or loading a local host


def month_spend(provider_id: str) -> float:
    start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    r = db.q("SELECT COALESCE(SUM(usd),0) AS s FROM spend WHERE provider=? AND ts>=?", provider_id, start)
    return float(r[0]["s"])


def record_spend(p: ProviderConfig, tokens_in: int, tokens_out: int, usd: float | None = None) -> float:
    if usd is None:
        usd = tokens_in / 1e6 * p.price_in_per_mtok + tokens_out / 1e6 * p.price_out_per_mtok
    db.x("INSERT INTO spend(ts,provider,usd,tokens_in,tokens_out) VALUES(?,?,?,?,?)",
         time.time(), p.id, usd, tokens_in, tokens_out)
    return usd


def _argv(cmd: str | list[str]) -> list[str]:
    return cmd if isinstance(cmd, list) else shlex.split(cmd, posix=os.name != "nt")


def _run(cmd: str | list[str], timeout: int) -> bool:
    argv = [a.strip('"') for a in _argv(cmd)]
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run(argv, timeout=timeout, capture_output=True, creationflags=flags)
        return r.returncode == 0
    except Exception as e:
        log.warning("command %s failed: %s", argv[0], e)
        return False


def _smi(smi: str, *args: str) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run([smi, *args], capture_output=True, text=True, timeout=10, creationflags=flags).stdout


_local_inflight = 0


@contextlib.contextmanager
def local_inflight():
    """Count a local inference request pmjc itself has in flight (see gpu_busy rule c)."""
    global _local_inflight
    _local_inflight += 1
    try:
        yield
    finally:
        _local_inflight -= 1


def local_requests() -> int:
    return _local_inflight


def _running(names: list[str]) -> bool:
    return bool(_matching(names))


def _matching(names: list[str]) -> list[str]:
    """Names of running processes that contain one of `names` (case-insensitive)."""
    needles = [n.lower() for n in names if n]
    if not needles:
        return []
    try:
        import psutil
        procs = [(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])]
    except Exception:
        try:
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10,
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout
                procs = [row[0].lower() for row in csv.reader(out.splitlines()) if row]
            else:
                out = subprocess.run(["ps", "-eo", "comm"], capture_output=True, text=True, timeout=10).stdout
                procs = [l.strip().lower() for l in out.splitlines()]
        except Exception:
            return []
    return sorted({name for name in procs if any(n in name for n in needles)})


def gpu_info(threshold: int, processes: list[str] | None = None) -> dict:
    """{busy, util, processes, why}: see gpu_busy for the rules. `processes` are the listed ones that are running."""
    if processes is None:
        processes = config.load().router.gpu_busy_processes
    out = {"busy": False, "util": None, "processes": [], "why": ""}
    found = _matching(processes)
    smi = shutil.which("nvidia-smi")
    util = None
    if smi:
        try:
            util = max(int(x) for x in _smi(smi, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits").split())
        except Exception:
            util = None
    out["util"] = util
    if found:
        out.update(busy=True, processes=found, why="a listed process is running")
    elif _local_inflight == 0 and util is not None and util >= threshold:
        out.update(busy=True, why="high utilization")
    return out


def gpu_busy(threshold: int, processes: list[str] | None = None) -> bool:
    """True when the GPU is busy with something other than pmjc's own local models. In order:
    (a) a process named in router.gpu_busy_processes (games, launchers) is running -> busy;
    (b) utilization at or over `threshold` percent while pmjc has no local request in flight -> busy;
    (c) high utilization while pmjc has local requests in flight -> not busy (the load can't be told apart from a
        game, so only the process list in (a) is trusted).
    Without nvidia-smi only (a) applies. Games are graphics apps, so nvidia-smi's compute-app list can't see them."""
    return gpu_info(threshold, processes)["busy"]


# Set while a run the owner chose to start on a busy GPU ("use local anyway") is going.
gpu_override: contextvars.ContextVar[bool] = contextvars.ContextVar("pmjc_gpu_override", default=False)


def api_key(p: ProviderConfig) -> str | None:
    return creds.store().get(p.credential) if p.credential else None


async def _models(p: ProviderConfig) -> list[str] | None:
    headers = {"Authorization": f"Bearer {api_key(p)}"} if p.credential else {}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(p.base_url.rstrip("/") + "/models", headers=headers)
            if r.status_code != 200:
                return None
            return [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:
        return None


async def _loaded(p: ProviderConfig) -> list[str] | None:
    """Models actually loaded in memory (LM Studio's /api/v0/models), or None if the host doesn't report it."""
    root = p.base_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(root + "/api/v0/models")
            if r.status_code != 200:
                return None
            return [m["id"] for m in r.json().get("data", []) if m.get("state") == "loaded"]
    except Exception:
        return None


async def healthy(p: ProviderConfig, start: bool = True) -> bool:
    start = start and config.load().auto_start_local
    if p.kind == "claude_cli":
        return shutil.which("claude") is not None and creds.store().has("claude.oauth_token")
    if p.kind == "anthropic":
        return bool(api_key(p))
    if p.credential and not api_key(p):
        return False
    if p.trust == "local" and p.extra.get("skip_when_gpu_busy_percent") and not gpu_override.get() and gpu_busy(int(p.extra["skip_when_gpu_busy_percent"])):
        log.info("%s skipped: GPU busy", p.id)
        return False
    models = await _models(p)
    if models is None and start and p.start:
        log.info("starting %s", p.id)
        await asyncio.to_thread(_run, p.start, p.start_timeout)
        deadline = time.time() + p.start_timeout
        while models is None and time.time() < deadline:
            await asyncio.sleep(3)
            models = await _models(p)
    if models is None:
        return False
    if not start and p.trust == "local":
        # Auto-start off: don't let the host load a model on demand (LM Studio JIT); use only what is loaded.
        loaded = await _loaded(p)
        if loaded is not None:
            if p.model:
                return p.model in loaded
            if not loaded:
                return False
            p.model = loaded[0]
            return True
    if p.model and p.model not in models:
        load = p.extra.get("load") or _lms_load(p)
        if not (start and p.load_model and load):
            return False
        log.info("loading %s on %s", p.model, p.id)
        await asyncio.to_thread(_run, load, max(p.start_timeout, 300))
        models = await _models(p) or []
        return p.model in models
    if not p.model:
        if not models:
            return False
        p.model = models[0]  # whatever is loaded
    return True


_starting: dict[str, asyncio.Task] = {}


def kick_start(p: ProviderConfig) -> bool:
    """Start/load a local host in the background (once at a time per provider). False if not started."""
    if not config.load().auto_start_local:
        return False
    t = _starting.get(p.id)
    if t is not None and not t.done():
        return False

    async def go():
        try:
            await healthy(p, start=True)
        except Exception:
            log.warning("background start of %s failed", p.id, exc_info=True)
    _starting[p.id] = asyncio.get_running_loop().create_task(go())
    return True


async def usable(p: ProviderConfig, need: Need) -> bool:
    """Healthy enough to run `need` now. Interactive requests don't wait for a local host: if it is down it is started
    in the background and the caller moves on to the next candidate. Jobs and watches keep the blocking start."""
    if not need.interactive:
        return await healthy(p)
    if await healthy(p, start=False):
        return True
    if p.trust == "local" and p.kind == "openai_compat":
        kick_start(p)
    return False


def _lms_load(p: ProviderConfig) -> list[str] | None:
    """`lms load <model>` for a local LM Studio host."""
    if "127.0.0.1" not in (p.base_url or "") and "localhost" not in (p.base_url or ""):
        return None
    exe = shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms" + (".exe" if os.name == "nt" else ""))
    if not os.path.exists(exe):
        return None
    ctx = ["--context-length", str(p.context_tokens)] if p.context_tokens else []  # JIT loads default to a small context
    return [exe, "load", p.model, *ctx, "-y"]


async def candidates(need: Need) -> list[ProviderConfig]:
    """Providers in preference order that pass the static checks (no health probe yet)."""
    routing = config.load().routing
    if need.order:
        by_id = {p.id: p for p in routing}
        routing = [by_id[i] for i in need.order if i in by_id]
    out = []
    for p in routing:
        if not p.enabled:
            continue
        if need.private and p.trust != "local":
            continue
        if not need.capabilities.issubset(set(p.capabilities)):
            continue
        if need.min_context and p.context_tokens < need.min_context:
            continue
        if p.monthly_budget_usd is not None and month_spend(p.id) >= p.monthly_budget_usd:
            continue
        if _cooldown.get(p.id, 0) > time.time():
            continue
        out.append(p)
    return out


def mark_failed(p: ProviderConfig) -> None:
    _cooldown[p.id] = time.time() + COOLDOWN_S


async def status() -> list[dict]:
    out = []
    for p in config.load().routing:
        out.append({"id": p.id, "kind": p.kind, "enabled": p.enabled, "trust": p.trust, "model": p.model,
                    "healthy": await healthy(p, start=False) if p.enabled else False,
                    "month_spend_usd": round(month_spend(p.id), 4), "monthly_budget_usd": p.monthly_budget_usd,
                    "cooling_down": _cooldown.get(p.id, 0) > time.time()})
    return out


def dumps(o) -> str:
    return json.dumps(o, default=str)
