"""Chat commands for the OpenAI-compatible front door (/v1): a user message starting with one of them is answered by
pmjc itself, with no model run (fast, deterministic, free). `/strong` and `/local` are routing prefixes, not commands;
an unknown `/something` goes to the model as normal text. All names live in COMMANDS."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from . import __version__, auth, config, db, memory, registry, router, routing

COMMANDS: dict[str, str] = {
    "help": "this list",
    "tools": "[filter]: the tools your token can use, by connector (read / act = needs approval)",
    "connectors": "connectors with setup and health, and their tool counts",
    "approvals": "pending approvals you may decide, with Approve / Deny links",
    "status": "version, uptime, providers, GPU, this thread's routing, today's spend",
    "model": "[tier|provider|auto]: show or set this thread's model choice",
    "threads": "recent threads",
    "new": "start a fresh thread for this conversation",
}
MAX_LINES = 60
_START = time.time()


@dataclass
class Ctx:
    caller: object  # the Caller the model run would use (ask:<token>)
    token: str  # the bearer token's caller name
    thread_id: str | None
    private: bool = False
    model: str = "pmjc"
    new_thread: bool = False  # set by /new: the door maps the conversation to ctx.thread_id
    created: bool = False  # a thread was created for this command
    notes: list[str] = field(default_factory=list)


def parse(text: str) -> tuple[str, str] | None:
    """(command, argument) if `text` starts with a chat command, else None."""
    head, _, rest = text.strip().partition(" ")
    if not head.startswith("/"):
        return None
    name = head[1:].lower()
    if name not in COMMANDS:
        return None
    return name, rest.strip()


def clip(s: str, n: int = 100) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def cap(lines: list[str], hint: str) -> list[str]:
    if len(lines) <= MAX_LINES:
        return lines
    return lines[:MAX_LINES] + [f"… {len(lines) - MAX_LINES} more, {hint}"]


# --- shared helpers (also usable by other front doors) -------------------------------------------------------
def tool_rows(caller, flt: str = "") -> tuple[dict[str, list[tuple[registry.ToolSpec, bool]]], int]:
    """Tools grouped by connector id, each with whether `caller` can use it. Returns (groups, hidden): without a
    filter, tools the caller can't use are left out and counted; with one they're included, marked."""
    from . import ask as ask_mod
    usable = {t.name for t in ask_mod.ask_tools(caller)}
    flt = flt.lower().strip()
    groups: dict[str, list[tuple[registry.ToolSpec, bool]]] = {}
    hidden = 0
    for t in registry.get().tools():
        if flt and flt not in t.connector.lower() and flt not in t.name.lower():
            continue
        ok = t.name in usable
        if not ok and not flt:
            hidden += 1
            continue
        groups.setdefault(t.connector, []).append((t, ok))
    return groups, hidden


def approval_line(a: dict) -> str:
    """One pending approval with signed Approve / Deny links (the format of the /v1 approvals block)."""
    base = config.load().public_url.rstrip("/")
    aid = a["id"]
    ok = f"{base}/approve/{aid}?sig={auth.sign('approve:' + aid)}"
    no = f"{base}/approve/{aid}?deny=1&sig={auth.sign('deny:' + aid)}"
    why = f" ({a['reason'][:200]})" if a.get("reason") else ""
    return f"- `{a['tool']}`{why}\n  [Approve]({ok}) / [Deny]({no})"


# --- commands -------------------------------------------------------------------------------------------------
def help_text() -> str:
    lines = ["**pmjc chat commands** (answered by pmjc, no model run):"]
    lines += [f"- `/{n}` {d}" for n, d in COMMANDS.items()]
    pf = [r.prefix for r in config.load().router.route_rules if r.prefix]
    if pf:
        lines.append("Routing prefixes (the model still answers): " + ", ".join(f"`{p}`" for p in dict.fromkeys(pf)))
    return "\n".join(lines)


def tools_text(ctx: Ctx, flt: str) -> str:
    groups, hidden = tool_rows(ctx.caller, flt)
    if not groups:
        return f"No tools match `{flt}`." if flt else "No tools available for this token."
    total = sum(len(v) for v in groups.values())
    lines = [f"**{total} tool(s)** in {len(groups)} connector(s). `read` runs now; `act` needs your approval."]
    for cid in sorted(groups):
        lines.append(f"\n**{cid}**")
        for t, ok in sorted(groups[cid], key=lambda x: x[0].name):
            kind = "read" if t.scope == "read" else ("jobs" if t.scope == "jobs" else "act")
            mark = "" if ok else ", no access"
            lines.append(f"- `{t.name}` ({kind}{mark}): {clip(registry.compact_tool_doc(t), 90)}")
    if hidden:
        lines.append(f"\n{hidden} more tool(s) need scopes this token lacks; `/tools <name>` lists them.")
    return "\n".join(cap(lines, "use /tools <filter>"))


async def _health(c) -> str:
    if not c.configured():
        return "needs setup"
    try:
        h = await asyncio.wait_for(c.health(), 3)
        return "ok" if h.ok else "unhealthy" + (f" ({clip(h.detail, 60)})" if h.detail else "")
    except asyncio.TimeoutError:
        return "no answer (3 s)"
    except Exception as e:
        return f"error ({type(e).__name__})"


async def connectors_text(ctx: Ctx) -> str:
    reg = registry.get()
    groups, _ = tool_rows(ctx.caller, "")
    counts: dict[str, int] = {}
    for t in reg.tools(include_unconfigured=True):
        counts[t.connector] = counts.get(t.connector, 0) + 1
    usable = {k: len(v) for k, v in groups.items()}
    conns = list(reg.connectors.values())
    states = await asyncio.gather(*[_health(c) for c in conns])
    lines = [f"**{len(conns)} connector(s)**"]
    for c, st in sorted(zip(conns, states), key=lambda x: x[0].id):
        lines.append(f"- `{c.id}`: {st}, {usable.get(c.id, 0)}/{counts.get(c.id, 0)} tools usable")
    for k, v in reg.errors.items():
        lines.append(f"- `{k}`: failed to load ({clip(v, 80)})")
    return "\n".join(cap(lines, "use /tools <filter>"))


def pending_for(ctx: Ctx) -> list[dict]:
    rows = db.q("SELECT id, tool, reason, requested_by FROM approvals WHERE status='pending' "
                "AND tool!='pmjc_route_override' AND (expires IS NULL OR expires>?) ORDER BY created", time.time())
    admin = ctx.caller.can("admin")
    mine = {ctx.caller.name, ctx.token}
    return [r for r in rows if admin or r["requested_by"] in mine]


def approvals_text(ctx: Ctx) -> str:
    rows = pending_for(ctx)
    if not rows:
        return "No pending approvals."
    return "\n".join(cap([f"**{len(rows)} pending approval(s)**"] + [approval_line(r) for r in rows], "open the dashboard"))


def route_row(tid: str | None) -> dict | None:
    if not tid:
        return None
    routing._init()
    r = db.q("SELECT provider, tier FROM thread_route WHERE thread_id=?", tid)
    return r[0] if r else None


def _spend_today() -> float:
    lt = time.localtime()
    start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    return float(db.q("SELECT COALESCE(SUM(usd),0) AS s FROM spend WHERE ts>=?", start)[0]["s"])


async def status_text(ctx: Ctx) -> str:
    up = int(time.time() - _START)
    h, m = divmod(up // 60, 60)
    lines = [f"**pmjc {__version__}**, up {h} h {m} min"]
    try:
        prov = await asyncio.wait_for(router.status(), 8)
    except Exception:
        prov = []
        lines.append("provider health unavailable")
    if prov:
        lines.append("**Providers** (order = priority)")
        for p in prov:
            st = "ok" if p["healthy"] else ("off" if not p["enabled"] else "down")
            cool = ", cooling down" if p.get("cooling_down") else ""
            lines.append(f"- {st} `{p['id']}` ({p['trust']}){cool}")
    thr = next((int(p.extra["skip_when_gpu_busy_percent"]) for p in config.load().routing
                if p.trust == "local" and p.extra.get("skip_when_gpu_busy_percent")), 90)
    try:
        g = await asyncio.wait_for(asyncio.to_thread(router.gpu_info, thr), 5)
        lines.append("GPU: " + (f"busy ({g['why']})" if g["busy"] else "free"))
    except Exception:
        lines.append("GPU: unknown")
    r = route_row(ctx.thread_id)
    lines.append("This thread: " + (f"sticky `{r['provider']}` (tier {r['tier']})" if r else "no sticky choice yet")
                 + (" (private: stays local)" if ctx.private else ""))
    try:
        lines.append(f"Spend today: ${_spend_today():.4f}")
    except Exception:
        pass
    return "\n".join(lines)


def _provider_trust() -> dict[str, str]:
    return {p.id: p.trust for p in config.load().routing if p.enabled}


def model_text(ctx: Ctx, arg: str) -> str:
    t = routing.tiers()
    trust = _provider_trust()
    if not arg:
        r = route_row(ctx.thread_id)
        lines = ["**Model for this thread:** " + (f"`{r['provider']}` (tier {r['tier']})" if r else "auto (pmjc picks)")]
        if ctx.private:
            lines.append("Private thread: only local models are used.")
        if ctx.model not in ("", "pmjc"):
            lines.append(f"The app's model `{ctx.model}` overrides this choice.")
        lines.append("Tiers: " + "; ".join(f"`{k}` ({', '.join(v) or 'none'})" for k, v in t.items()))
        lines.append("Providers: " + ", ".join(f"`{i}`" for i in trust))
        lines.append("Set with `/model <tier|provider>`; `/model auto` clears it.")
        return "\n".join(lines)
    name = arg.split()[0]
    if name.lower() == "auto":
        if ctx.thread_id:
            routing._init()
            db.x("DELETE FROM thread_route WHERE thread_id=?", ctx.thread_id)
        return "Model for this thread: auto."
    if name in t and t[name]:
        ids, tier = list(t[name]), name
    elif name in trust:
        ids, tier = [name], next((k for k, v in t.items() if name in v and k != "default"), "default")
    else:
        return f"Unknown model `{name}`. Tiers: {', '.join(t)}. Providers: {', '.join(trust)}. Or `auto`."
    if ctx.private:  # never widen: a private thread stays on local providers
        ids = [i for i in ids if trust.get(i) == "local"]
        if not ids:
            return f"`{name}` has no local provider, and this thread is private (it stays local)."
    if not config.load().router.sticky:
        return "Sticky routing is off (router.sticky), so a per-thread choice can't be kept."
    if not ctx.thread_id:
        ctx.thread_id = memory.thread_new("Chat", by=ctx.caller.name, private=ctx.private)
        ctx.created = True
    routing._init()
    prov = ids[0]
    db.x("INSERT INTO thread_route(thread_id,provider,tier,ts,planned_provider,planned_tier,fell_back,reason) "
         "VALUES(?,?,?,?,?,?,0,'') ON CONFLICT(thread_id) DO UPDATE SET provider=excluded.provider, tier=excluded.tier, "
         "ts=excluded.ts, planned_provider=excluded.planned_provider, planned_tier=excluded.planned_tier, "
         "fell_back=0, reason=''", ctx.thread_id, prov, tier, time.time(), prov, tier)
    extra = f" The app's model `{ctx.model}` still overrides it." if ctx.model not in ("", "pmjc") else ""
    return f"Model for this thread: `{prov}` (tier {tier}). `/model auto` clears it.{extra}"


def threads_text(ctx: Ctx) -> str:
    rows = memory.threads_recent(limit=MAX_LINES + 1)
    if not rows:
        return "No threads yet."
    lines = [f"**Recent threads** ({min(len(rows), MAX_LINES)}" + ("+" if len(rows) > MAX_LINES else "") + ")"]
    for r in rows[:MAX_LINES]:
        flags = ", ".join(x for x in (r["status"], "unread" if r["unread"] else "", "private" if r["private"] else "") if x)
        lines.append(f"- `{r['id']}` {clip(r['title'], 60)} ({flags})")
    if len(rows) > MAX_LINES:
        lines.append("… more in the dashboard")
    return "\n".join(lines)


def new_text(ctx: Ctx) -> str:
    ctx.thread_id = memory.thread_new("New chat", by=ctx.caller.name, private=ctx.private)
    ctx.new_thread = ctx.created = True
    return f"New thread `{ctx.thread_id}` started. Your next message starts clean."


async def run(name: str, arg: str, ctx: Ctx) -> str:
    if name == "help":
        return help_text()
    if name == "tools":
        return tools_text(ctx, arg)
    if name == "connectors":
        return await connectors_text(ctx)
    if name == "approvals":
        return approvals_text(ctx)
    if name == "status":
        return await status_text(ctx)
    if name == "model":
        return model_text(ctx, arg)
    if name == "threads":
        return threads_text(ctx)
    return new_text(ctx)
