"""Eval runner: each task runs in a throwaway PMJC_HOME with fake connectors, on a real (or fake-served) provider.

Isolation: the temporary home gets only the selected provider's settings (with overrides applied) and, if that
provider names one, its API key. The real settings, database, connectors and other credentials are never opened,
and nothing is written to the real home unless the caller saves the report there (`save_report`).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets as _secrets
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import httpx
import yaml

from .. import agent, ask, auth, config, creds, db, executor, memory, registry, router
from ..config import ProviderConfig, Settings
from . import fakes, score
from .tasks import Task

UNSUPPORTED_KINDS = {"claude_cli"}  # needs a running pmjc server and the plan's login: not isolatable


class EvalError(Exception):
    pass


# --- providers and overrides ---------------------------------------------------------------------------
def real_providers() -> list[ProviderConfig]:
    """The provider list from the real settings.yaml, read-only (nothing is created if the file is missing)."""
    f = config.home() / "settings.yaml"
    if not f.exists():
        return config.default_providers()
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    routing = data.get("routing")
    if routing is None:
        return config.default_providers()
    return [ProviderConfig.model_validate(x) for x in routing]


def parse_overrides(items: list[str] | None) -> dict[str, Any]:
    """`schema_mode=compact`, `context_tokens=8000`, `extra.skip_when_gpu_busy_percent=50` -> a dict (values parsed as YAML)."""
    out: dict[str, Any] = {}
    for it in items or []:
        k, sep, v = it.partition("=")
        if not sep or not k.strip():
            raise EvalError(f"override {it!r} must look like key=value")
        out[k.strip()] = yaml.safe_load(v) if v.strip() != "" else ""
    return out


def apply_overrides(p: ProviderConfig, overrides: dict[str, Any]) -> ProviderConfig:
    data = p.model_dump()
    for k, v in overrides.items():
        if k.startswith("extra."):
            data["extra"] = {**data.get("extra", {}), k[6:]: v}
        elif k in ("id", "kind", "credential", "start", "base_url", "trust"):
            raise EvalError(f"override of {k!r} is not allowed (it could point the eval at another host or credential)")
        elif k not in ProviderConfig.model_fields:
            raise EvalError(f"unknown provider option {k!r}")
        else:
            data[k] = v
    data["enabled"] = True
    return ProviderConfig.model_validate(data)


def is_paid(p: ProviderConfig) -> bool:
    return p.trust != "local"


@dataclass
class Variant:
    label: str = "default"
    overrides: dict[str, Any] = field(default_factory=dict)


def parse_variant(text: str) -> Variant:
    """`compact:schema_mode=compact,context_tokens=8000` -> Variant("compact", {...})."""
    label, sep, rest = text.partition(":")
    if not sep:
        raise EvalError(f"variant {text!r} must look like label:key=value,key=value")
    return Variant(label.strip(), parse_overrides([x for x in rest.split(",") if x.strip()]))


def read_secrets(providers: list[ProviderConfig]) -> dict[str, str]:
    """The API key of each selected provider, read from the real credential store (the only thing copied)."""
    out: dict[str, str] = {}
    for p in providers:
        if p.credential:
            with contextlib.suppress(Exception):
                v = creds.store().get(p.credential)
                if v:
                    out[p.credential] = v
    return out


# --- isolated home ---------------------------------------------------------------------------------------
def _reset_singletons() -> None:
    c = getattr(db._local, "c", None)
    if c is not None:
        with contextlib.suppress(Exception):
            c.close()
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    router._cooldown.clear()


def eval_settings(p: ProviderConfig) -> Settings:
    s = Settings(routing=[p])
    s.credential_backend = "file"
    return s


@contextlib.contextmanager
def isolated_home(provider: ProviderConfig, secrets: dict[str, str] | None = None) -> Iterator[Path]:
    """Point PMJC_HOME at a fresh temp folder holding only `provider`, install the fake connectors, seed contacts."""
    from ..connectors.pmjc_system import Pmjc
    saved = {k: os.environ.get(k) for k in ("PMJC_HOME", "PMJC_MASTER_KEY")}
    _reset_singletons()
    try:
        with tempfile.TemporaryDirectory(prefix="pmjc-eval-", ignore_cleanup_errors=True) as d:
            os.environ["PMJC_HOME"] = d
            os.environ["PMJC_MASTER_KEY"] = _secrets.token_hex(16)
            try:
                config.save(eval_settings(provider))
                for k, v in (secrets or {}).items():
                    creds.store().set(k, v)
                fakes.reset()
                from .. import watches
                db.x(watches.DDL)  # ask() looks at the watches table; the server normally creates it
                reg = registry.Registry()
                for cls in (Pmjc, *fakes.FAKE_CONNECTORS):
                    reg.connectors[cls.id] = cls()
                registry._reg = reg
                for c in fakes.CONTACTS:
                    memory.contact_set(c["name"], c["aliases"], c["handles"])
                yield Path(d)
            finally:
                _reset_singletons()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --- metering (turns and tokens), without touching agent.py -------------------------------------------------
@dataclass
class Meter:
    turns: int = 0
    aux: int = 0  # calls without tools (condense passes)
    tokens_in: int = 0
    tokens_out: int = 0
    reported: bool = False


@contextlib.contextmanager
def metered() -> Iterator[Meter]:
    """Count the model calls of this process while active: HTTP POSTs to a chat-completions or messages endpoint."""
    cls = httpx._client.AsyncClient  # the real class, even if a test swapped httpx.AsyncClient for a factory
    orig = cls.send
    m = Meter()

    async def send(self, request, *a, **kw):
        resp = await orig(self, request, *a, **kw)
        try:
            if request.method == "POST" and request.url.path.endswith(("/chat/completions", "/messages")):
                body = json.loads(request.content or b"{}")
                if "tools" in body:
                    m.turns += 1
                else:
                    m.aux += 1
                u = (resp.json() or {}).get("usage") or {}
                tin = u.get("prompt_tokens", u.get("input_tokens"))
                tout = u.get("completion_tokens", u.get("output_tokens"))
                if tin is not None or tout is not None:
                    m.reported = True
                    m.tokens_in += int(tin or 0)
                    m.tokens_out += int(tout or 0)
        except Exception:
            pass
        return resp

    cls.send = send
    try:
        yield m
    finally:
        cls.send = orig


# --- one run -----------------------------------------------------------------------------------------------
@dataclass
class RunResult:
    provider: str
    variant: str
    set: str
    task: str
    category: str
    repeat: int
    skipped: str = ""
    score: float = 0.0
    checks: list[dict] = field(default_factory=list)
    turns: int = 0
    latency_s: float = 0.0
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float = 0.0
    fell_back: list[str] = field(default_factory=list)
    error: str = ""
    answer: str = ""
    calls: list[dict] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.provider if self.variant == "default" else f"{self.provider}/{self.variant}"


def _seed_thread(task: Task) -> str | None:
    if not task.thread:
        return None
    tid = memory.thread_new(task.thread[0].user[:80], by="eval")
    for t in task.thread:
        memory.thread_add(tid, "request", t.user, author="eval", trust="owner")
        memory.thread_add(tid, "answer", t.assistant, author="eval", trust="agent")
    return tid


async def _decide_approvals(task: Task, thread_id: str) -> None:
    """Approvals never wait for a person: decide per the task spec. A shell command is never approved."""
    for a in db.q("SELECT id, tool FROM approvals WHERE thread_id=? AND status='pending'", thread_id):
        approve = task.approve and a["tool"] != "pmjc_shell_run"
        try:
            await executor.decide(a["id"], approve, "eval")
        except Exception:
            pass


async def run_task(task: Task, provider: ProviderConfig, secrets: dict[str, str] | None = None, variant: str = "default",
                   repeat: int = 1, timeout: float = 600) -> RunResult:
    r = RunResult(provider.id, variant, task.set, task.id, task.category, repeat)
    with isolated_home(provider, secrets):
        reg = registry.get()
        missing = [n for n in task.requires_tools if reg.tool(n) is None]
        if missing:
            r.skipped = "needs " + ", ".join(missing)
            return r
        tr = score.Transcript(meta={t.name: score.ToolMeta(t.scope, t.untrusted_output, t.egress)
                                    for t in reg.tools(include_unconfigured=True)})
        caller = auth.Caller("eval", "local", {"sources", "jobs", "actions"})

        async def hook(name: str, args: dict, status: str) -> None:
            tr.calls.append(score.Call(name, dict(args), status))

        caller.on_tool = hook
        tid = _seed_thread(task)
        t0 = time.perf_counter()
        with metered() as m:
            try:
                out = await asyncio.wait_for(
                    ask.ask(caller, task.prompt, provider_order=[provider.id], thread_id=tid,
                            history_turns=len(task.thread), extra_system=fakes.EVAL_SYSTEM), timeout)
                tr.answer = out["answer"]
                r.cost_usd = out.get("cost_usd") or 0.0
                r.fell_back = list(out.get("fell_back") or [])
                await _decide_approvals(task, out["thread_id"])
            except asyncio.TimeoutError:
                tr.error = f"timeout after {timeout:g}s"
            except Exception as e:
                tr.error = f"{type(e).__name__}: {e}"[:500]
        r.latency_s = time.perf_counter() - t0
        tr.turns = m.turns
        tr.executed = list(fakes.STATE.executed)
        r.turns = m.turns
        if m.reported:
            r.tokens_in, r.tokens_out = m.tokens_in, m.tokens_out
        checks = score.score_task(task, tr)
        r.checks = [c.__dict__ for c in checks]
        r.score = score.task_score(checks)
        r.error = tr.error or ""
        r.answer = tr.answer[:2000]
        r.calls = [c.__dict__ for c in tr.calls]
        return r


# --- paid-provider estimate ----------------------------------------------------------------------------------
EST_TURNS = 4  # assumed model calls per task
EST_PROMPT_EXTRA = 900  # tokens of prompt per call besides the tool list and system prompt (history, tool results)
EST_OUT = 200  # output tokens per call


def estimate(tasks: list[Task], provider: ProviderConfig, repeat: int, n_variants: int = 1) -> dict:
    """Rough token and cost estimate for running `tasks` on `provider` (shown before a paid run)."""
    with isolated_home(provider):
        caller = auth.Caller("eval", "local", {"sources", "jobs", "actions"})
        tools = agent._with_paging(caller, ask.ask_tools(caller))
        schema, system = agent.tool_payload(provider, tools, ask.ASK_SYSTEM, "openai")
        fixed = agent._tokens(schema) + agent._tokens(system)
        reg = registry.get()
        runnable = [t for t in tasks if all(reg.tool(n) is not None for n in t.requires_tools)]
    runs = len(runnable) * repeat * n_variants
    tin = runs * EST_TURNS * (fixed + EST_PROMPT_EXTRA)
    tout = runs * EST_TURNS * EST_OUT
    usd = tin / 1e6 * provider.price_in_per_mtok + tout / 1e6 * provider.price_out_per_mtok
    return {"provider": provider.id, "runs": runs, "tokens_in": tin, "tokens_out": tout, "usd": round(usd, 4),
            "priced": bool(provider.price_in_per_mtok or provider.price_out_per_mtok)}


# --- the whole eval ------------------------------------------------------------------------------------------
async def run_eval(tasks: list[Task], providers: list[ProviderConfig], variants: list[Variant] | None = None,
                   repeat: int = 1, timeout: float = 600, secrets: dict[str, str] | None = None,
                   budget_usd: float | None = None, progress=None) -> dict:
    """Run every task `repeat` times on every provider x variant. Returns the report dict (rows + runs)."""
    variants = variants or [Variant()]
    for p in providers:
        if p.kind in UNSUPPORTED_KINDS:
            raise EvalError(f"provider {p.id} (kind {p.kind}) can't run in an isolated home")
    started = datetime.now().astimezone()
    runs: list[RunResult] = []
    unavailable: dict[str, str] = {}
    spent = 0.0
    stopped = ""
    say = progress or (lambda s: None)
    for v in variants:
        for base in providers:
            p = apply_overrides(base, v.overrides)
            key = p.id if v.label == "default" else f"{p.id}/{v.label}"
            with isolated_home(p, secrets):
                ok = await router.healthy(p, start=False)
            if not ok:
                unavailable[key] = "provider not reachable or its model is not loaded"
                say(f"{key}: unavailable, skipped")
                continue
            for task in tasks:
                for rep in range(1, repeat + 1):
                    if budget_usd is not None and spent >= budget_usd:
                        stopped = f"budget of ${budget_usd:g} reached"
                        break
                    r = await run_task(task, p, secrets, v.label, rep, timeout)
                    spent += r.cost_usd
                    runs.append(r)
                    flag = "skipped (%s)" % r.skipped if r.skipped else f"score {r.score:.2f}" + (f" ERROR {r.error[:80]}" if r.error else "")
                    say(f"{key} {task.id}#{rep}: {flag}")
                if stopped:
                    break
            if stopped:
                break
        if stopped:
            break
    return {"created": started.isoformat(timespec="seconds"), "repeat": repeat,
            "variants": [{"label": v.label, "overrides": v.overrides} for v in variants],
            "rows": summarize(runs, unavailable), "runs": [r.__dict__ for r in runs],
            "spent_usd": round(spent, 5), "stopped": stopped}


def summarize(runs: list[RunResult], unavailable: dict[str, str] | None = None) -> list[dict]:
    keys: dict[str, list[RunResult]] = {}
    for r in runs:
        keys.setdefault(r.key, []).append(r)
    rows = []
    for key, rs in keys.items():
        done = [r for r in rs if not r.skipped]
        n = len(done)
        tin = [r.tokens_in for r in done if r.tokens_in is not None]
        tout = [r.tokens_out for r in done if r.tokens_out is not None]
        rows.append({
            "key": key, "provider": rs[0].provider, "variant": rs[0].variant, "runs": n,
            "skipped": len(rs) - n,
            "mean_score": round(sum(r.score for r in done) / n, 4) if n else None,
            "passed": sum(1 for r in done if r.score >= 1.0 and not r.error),
            "mean_turns": round(sum(r.turns for r in done) / n, 2) if n else None,
            "mean_latency_s": round(sum(r.latency_s for r in done) / n, 2) if n else None,
            "tokens_in": sum(tin) if tin else None, "tokens_out": sum(tout) if tout else None,
            "fallbacks": sum(1 for r in done if r.fell_back),
            "errors": sum(1 for r in done if r.error),
            "cost_usd": round(sum(r.cost_usd for r in done), 5),
        })
    for key, why in (unavailable or {}).items():
        rows.append({"key": key, "provider": key.split("/")[0], "variant": key.partition("/")[2] or "default", "runs": 0,
                     "skipped": 0, "mean_score": None, "passed": 0, "mean_turns": None, "mean_latency_s": None,
                     "tokens_in": None, "tokens_out": None, "fallbacks": 0, "errors": 0, "cost_usd": 0.0,
                     "unavailable": why})
    return rows


# --- output ------------------------------------------------------------------------------------------------
def _f(v, fmt="{}", dash="-") -> str:
    return dash if v is None else fmt.format(v)


def render_markdown(report: dict) -> str:
    lines = [f"## Eval results ({report['created']}, repeat {report['repeat']})", "",
             "| provider | tasks | mean score | passed | mean turns | mean latency | tokens in/out | fallbacks | errors |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in report["rows"]:
        if r.get("unavailable"):
            lines.append(f"| {r['key']} | - | unavailable: {r['unavailable']} | | | | | | |")
            continue
        tok = "-" if r["tokens_in"] is None else f"{r['tokens_in']}/{r['tokens_out']}"
        lines.append(f"| {r['key']} | {r['runs']}" + (f" (+{r['skipped']} skipped)" if r["skipped"] else "")
                     + f" | {_f(r['mean_score'], '{:.3f}')} | {r['passed']}/{r['runs']} | {_f(r['mean_turns'])} | "
                     f"{_f(r['mean_latency_s'], '{}s')} | {tok} | {r['fallbacks']} | {r['errors']} |")
    runs = report["runs"]
    keys = [r["key"] for r in report["rows"] if not r.get("unavailable")]
    if runs and keys:
        def rkey(x):
            return x["provider"] if x["variant"] == "default" else f"{x['provider']}/{x['variant']}"
        tasks = list(dict.fromkeys((x["set"], x["task"]) for x in runs))
        lines += ["", "### Per task (mean score)", "", "| set | task | " + " | ".join(keys) + " |",
                  "|---|---|" + "---|" * len(keys)]
        for s, t in tasks:
            cells = []
            for k in keys:
                rs = [x for x in runs if x["set"] == s and x["task"] == t and rkey(x) == k]
                if not rs:
                    cells.append("-")
                elif all(x["skipped"] for x in rs):
                    cells.append("skipped")
                else:
                    done = [x for x in rs if not x["skipped"]]
                    cells.append(f"{sum(x['score'] for x in done) / len(done):.2f}")
            lines.append(f"| {s} | {t} | " + " | ".join(cells) + " |")
        fails = [x for x in runs if not x["skipped"] and (x["score"] < 1.0 or x["error"])]
        if fails:
            lines += ["", "### Failed checks", ""]
            for x in fails:
                bad = [c["name"] for c in x["checks"] if not c["passed"]]
                lines.append(f"- {rkey(x)} {x['task']}#{x['repeat']}: " + (f"ERROR {x['error'][:120]}" if x["error"]
                                                                          else "; ".join(bad)))
    if report.get("stopped"):
        lines += ["", f"Stopped early: {report['stopped']}."]
    return "\n".join(lines)


def save_report(report: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def default_save_path(home: Path) -> Path:
    return home / "evals" / (datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
