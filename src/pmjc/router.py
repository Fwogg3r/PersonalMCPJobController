"""Model router: picks the first usable provider from the user's ordered list.

Order and settings come from settings.yaml (dashboard -> Routing). A provider is skipped when it is disabled,
lacks a capability the job needs, is cloud while the job is private, is over its monthly budget, is cooling
down after a failure, or is unhealthy and can't be started.

Local OpenAI-compatible hosts (LM Studio, Ollama, ...) are started on demand with their `start` command
(argv, no shell; only editable in settings.yaml, never through the API), and LM Studio models are loaded
with `lms load`. A GPU guard skips local hosts while the GPU is busy (e.g. a game is running). A model that is
already loaded with a smaller context than the provider's `context_tokens` (e.g. LM Studio's JIT load at its small
default, started by another client) is reloaded with the right context before the run (`ensure_context`).
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
        if r.returncode != 0:
            err = (r.stderr or r.stdout or b"").decode("utf-8", "replace").strip()[-300:]
            log.warning("command %s exited %s: %s", argv[0], r.returncode, err)
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
        _phase[p.id] = "starting the model server"
        await asyncio.to_thread(_run, p.start, p.start_timeout)
        deadline = time.time() + p.start_timeout
        while models is None and time.time() < deadline:
            await asyncio.sleep(3)
            models = await _models(p)
    if models is None:
        return False
    if start and p.trust == "local" and p.model and p.model in models:
        # LM Studio's /v1/models lists every downloaded model, loaded or not; check what is really in memory so a
        # running server with nothing loaded gets the model loaded (not reported ready and then found unusable).
        loaded = await _loaded(p)
        if loaded is not None and p.model not in loaded:
            models = loaded
    if not start and p.trust == "local":
        # Auto-start off: don't let the host load a model on demand (LM Studio JIT); use only what is loaded.
        loaded = await _loaded(p)
        if loaded is not None:
            if p.model:
                if p.model not in loaded:
                    return False
                # Loaded with too small a context: not ready if a (background) start may reload it.
                return not (await context_short(p) and _can_reload(p))
            if not loaded:
                return False
            p.model = loaded[0]
            return True
    if p.model and p.model not in models:
        load = p.extra.get("load") or _lms_load(p)
        if not (start and p.load_model and load):
            return False
        log.info("loading %s on %s", p.model, p.id)
        _phase[p.id] = f"loading {p.model}"
        await asyncio.to_thread(_run, load, max(p.start_timeout, 300))
        if p.trust == "local":
            loaded = await _loaded(p)
            if loaded is not None:
                return p.model in loaded
        models = await _models(p) or []
        return p.model in models
    if not p.model:
        if not models:
            return False
        p.model = models[0]  # whatever is loaded
    if start and p.trust == "local" and p.context_tokens:
        if await ensure_context(p) == "failed":
            return False
    return True


_starting: dict[str, asyncio.Task] = {}
_phase: dict[str, str] = {}  # provider id -> what its background start is doing now (for progress notices)


def start_phase(p: ProviderConfig) -> str:
    return _phase.get(p.id, "starting")


def kick_start(p: ProviderConfig) -> bool:
    """Start/load a local host in the background (once at a time per provider). False if not started."""
    if not config.load().auto_start_local:
        return False
    t = _starting.get(p.id)
    if t is not None and not t.done():
        return False

    async def go():
        _phase[p.id] = "checking the model server"
        try:
            ok = await healthy(p, start=True)
            if not ok:
                log.warning("background start of %s finished but it is not ready", p.id)
        except Exception:
            log.warning("background start of %s failed", p.id, exc_info=True)
        finally:
            _phase.pop(p.id, None)
    _starting[p.id] = asyncio.get_running_loop().create_task(go())
    return True


async def wait_ready(p: ProviderConfig, seconds: float, on_progress=None, every: float = 15.0) -> bool:
    """Start the local host/model (one shared start task per provider; concurrent callers all wait on it) and wait
    until that start finishes, at most `seconds`. Every `every` seconds `on_progress(phase, elapsed_s)` is awaited.
    False if auto_start_local is off, the start failed, or time ran out."""
    if seconds <= 0 or not config.load().auto_start_local:
        return False
    t = _starting.get(p.id)
    if t is None or t.done():
        kick_start(p)
        t = _starting.get(p.id)
    if t is None:
        return False
    t0 = time.monotonic()
    while not t.done():
        left = seconds - (time.monotonic() - t0)
        if left <= 0:
            return False
        await asyncio.wait({t}, timeout=min(left, every) if every > 0 else left)
        if not t.done() and on_progress and time.monotonic() - t0 < seconds:
            try:
                await on_progress(start_phase(p), time.monotonic() - t0)
            except Exception:
                log.debug("progress notice failed", exc_info=True)
    if t.cancelled() or t.exception() is not None:
        return False
    return await healthy(p, start=False)


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


def _lms_exe(p: ProviderConfig) -> str | None:
    """The `lms` CLI, only for a host on this machine (it can't reach a remote LM Studio)."""
    if "127.0.0.1" not in (p.base_url or "") and "localhost" not in (p.base_url or ""):
        return None
    exe = shutil.which("lms") or os.path.expanduser("~/.lmstudio/bin/lms" + (".exe" if os.name == "nt" else ""))
    return exe if os.path.exists(exe) else None


def _lms_load(p: ProviderConfig) -> list[str] | None:
    """`lms load <model>` for a local LM Studio host."""
    exe = _lms_exe(p)
    if not exe:
        return None
    ctx = ["--context-length", str(p.context_tokens)] if p.context_tokens else []  # JIT loads default to a small context
    par = ["--parallel", str(p.parallel)] if p.parallel else []
    return [exe, "load", p.model, *ctx, *par, "-y"]


# --- context check: a model loaded with a smaller context than the provider's context_tokens ------------------
CONTEXT_ERRORS = ("exceed_context_size", "exceeds the available context size", "greater than the context length")
RELOAD_BUSY_WAIT_S = 60.0  # how long to wait for another client's generation to finish before reloading anyway
_reload_locks: dict[str, asyncio.Lock] = {}


def context_error(text: str) -> bool:
    """True when a host's error says the prompt didn't fit the loaded context (LM Studio's exceed_context_size_error)."""
    t = (text or "").lower()
    return any(e in t for e in CONTEXT_ERRORS)


def _lms_ps(p: ProviderConfig) -> list[dict] | None:
    """`lms ps --json`: the loaded instances (identifier, modelKey, contextLength, status, ...), None if unavailable."""
    exe = _lms_exe(p)
    if not exe:
        return None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run([exe, "ps", "--json"], capture_output=True, text=True, timeout=15,
                             creationflags=flags).stdout
        data = json.loads(out or "null")
        return data if isinstance(data, list) else None
    except Exception:
        return None


def _instance(rows: list[dict] | None, model: str) -> dict | None:
    for r in rows or []:
        if model in (r.get("identifier"), r.get("modelKey"), r.get("id")):
            return r
    return None


async def loaded_context(p: ProviderConfig) -> int | None:
    """Context length of the loaded instance of `p.model`: LM Studio's /api/v0/models `loaded_context_length`, else
    `lms ps --json` `contextLength`. None when the model isn't loaded or the host doesn't say."""
    if not p.model:
        return None
    root = p.base_url.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(root + "/api/v0/models")
            if r.status_code != 200:
                return None
            rows = r.json().get("data", [])
    except Exception:
        return None
    m = next((m for m in rows if m.get("id") == p.model and m.get("state") == "loaded"), None)
    if m is None:
        return None
    if m.get("loaded_context_length"):
        return int(m["loaded_context_length"])
    inst = _instance(await asyncio.to_thread(_lms_ps, p), p.model)  # older LM Studio: ask the CLI
    n = (inst or {}).get("contextLength") or (inst or {}).get("context_length")
    return int(n) if n else None


async def context_short(p: ProviderConfig) -> int | None:
    """The loaded context length when it is below `p.context_tokens`, else None."""
    if not (p.trust == "local" and p.kind == "openai_compat" and p.model and p.context_tokens):
        return None
    have = await loaded_context(p)
    return have if have is not None and have < p.context_tokens else None


def _can_reload(p: ProviderConfig) -> bool:
    return bool(config.load().auto_start_local and p.load_model and _lms_exe(p)
                and (p.extra.get("load") or _lms_load(p)))


def _busy_elsewhere(p: ProviderConfig) -> bool:
    """`lms ps` reports the instance as working (generating, processing a prompt, queued) for some client."""
    inst = _instance(_lms_ps(p), p.model)
    if not inst:
        return False
    status = str(inst.get("status") or "").lower()
    return bool(status and status not in ("idle", "loaded", "ready")) or bool(inst.get("queued"))


async def ensure_context(p: ProviderConfig) -> str:
    """Reload `p.model` when it is loaded with a smaller context than `p.context_tokens`.
    Returns "ok" (nothing to do), "reloaded" (now at the right context), "short" (still short: reloading is off,
    `lms` is missing, the GPU guard says busy, or pmjc has a request in flight on it; it is used as is) or "failed"
    (the reload left the model not loaded). Another client's generation is waited for (RELOAD_BUSY_WAIT_S), and
    unloading it while it is still busy is logged."""
    have = await context_short(p)
    if have is None:
        return "ok"
    why = f"{p.model} on {p.id} is loaded with a {have}-token context, below context_tokens {p.context_tokens}"
    if not _can_reload(p):
        log.warning("%s; not reloading (auto_start_local or load_model off, or no lms CLI); using it as is", why)
        return "short"
    thr = p.extra.get("skip_when_gpu_busy_percent")
    if thr and not gpu_override.get() and gpu_busy(int(thr)):
        log.warning("%s; not reloading: GPU busy", why)
        return "short"
    lock = _reload_locks.setdefault(p.id, asyncio.Lock())
    async with lock:
        have = await context_short(p)  # another task may have reloaded it while this one waited
        if have is None:
            return "ok" if await loaded_context(p) is not None or p.model in (await _loaded(p) or []) else "failed"
        if _local_inflight:
            log.warning("%s; not reloading: pmjc has %d local request(s) in flight", why, _local_inflight)
            return "short"
        t0 = time.monotonic()
        while await asyncio.to_thread(_busy_elsewhere, p):
            if time.monotonic() - t0 >= RELOAD_BUSY_WAIT_S:
                log.warning("%s; another client is still using it after %.0f s; unloading it anyway",
                            why, RELOAD_BUSY_WAIT_S)
                break
            await asyncio.sleep(3)
        log.warning("%s; reloading it with %d tokens", why, p.context_tokens)
        prev = _phase.get(p.id)
        _phase[p.id] = f"reloading {p.model} with a {p.context_tokens}-token context"
        try:
            await asyncio.to_thread(_run, [_lms_exe(p), "unload", p.model], 120)
            await asyncio.to_thread(_run, p.extra.get("load") or _lms_load(p), max(p.start_timeout, 300))
        finally:
            if prev is None:
                _phase.pop(p.id, None)
            else:
                _phase[p.id] = prev
        now = await loaded_context(p)
        if now is None:
            if p.model in (await _loaded(p) or []):
                return "reloaded"  # the host doesn't report the context; trust the load command
            log.warning("reload of %s on %s left it not loaded", p.model, p.id)
            return "failed"
        if now < p.context_tokens:
            log.warning("reload of %s on %s gave a %d-token context, still below %d", p.model, p.id, now,
                        p.context_tokens)
            return "short"
        return "reloaded"


async def candidates(need: Need) -> list[ProviderConfig]:
    """Providers in preference order that pass the static checks (no health probe yet)."""
    routing = config.load().routing
    if need.order:
        by_id = {p.id: p for p in routing}
        routing = [by_id[i] for i in need.order if i in by_id]
    local_tier = None
    if need.private:
        from . import routing as routing_mod  # (routing imports router)
        local_tier = set(routing_mod.tiers().get("local") or [])
    out = []
    for p in routing:
        if not p.enabled:
            continue
        if need.private and (p.trust != "local" or p.id not in local_tier):
            continue  # private work runs only on the `local` tier (by default every local provider)
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
