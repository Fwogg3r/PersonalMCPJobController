"""Front-door routing: tiers, route rules, sticky provider per thread, routing log, answer footer.

Decided in docs/DECISIONS.md ("Front-door routing"). Rules only choose the ORDER in which providers are tried; the
hard constraints (private = local only, capabilities, budget, cooldown, health) are enforced by router.candidates()
and re-checked in `apply`, so routing can never widen the candidate set.

Order of precedence: explicit override (model id / channel provider, or a rule with a `prefix`) > the thread's sticky
provider > the first matching other rule > the `default` tier.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from . import config, db, router

log = logging.getLogger("pmjc.routing")

DDL = """
CREATE TABLE IF NOT EXISTS routing (
  id INTEGER PRIMARY KEY, ts REAL, thread_id TEXT, surface TEXT, rule TEXT, tier TEXT, candidates TEXT, provider TEXT,
  fallbacks TEXT, turns INTEGER, tool_errors INTEGER, tools TEXT, cost_usd REAL, retry_of INTEGER, override INTEGER,
  error TEXT, tool_calls INTEGER DEFAULT 0, decision TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS routing_thread ON routing(thread_id, ts);
CREATE TABLE IF NOT EXISTS thread_route (thread_id TEXT PRIMARY KEY, provider TEXT, tier TEXT, ts REAL,
  planned_provider TEXT DEFAULT '', planned_tier TEXT DEFAULT '', fell_back INTEGER DEFAULT 0, reason TEXT DEFAULT '');
"""
INTERACTIVE = {"discord", "rest", "cli", "v1"}
FOOTER_MARK = "⟡"  # a glyph that does not occur in normal prose; the footer line starts with it
FOOTER_RE = re.compile(r"(?:\n[ \t]*)*(?:-# )?" + FOOTER_MARK + r" [^\n]*\Z")
_warned: set[str] = set()


def _init() -> None:
    c = db.conn()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='routing'").fetchone():
        c.executescript(DDL)
        return
    for table, col, decl in (("routing", "tool_calls", "INTEGER DEFAULT 0"), ("routing", "decision", "TEXT DEFAULT ''"),
                             ("thread_route", "planned_provider", "TEXT DEFAULT ''"),
                             ("thread_route", "planned_tier", "TEXT DEFAULT ''"),
                             ("thread_route", "fell_back", "INTEGER DEFAULT 0"), ("thread_route", "reason", "TEXT DEFAULT ''")):
        if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    c.commit()


# --- tiers ---------------------------------------------------------------------------------------------------
def tiers() -> dict[str, list[str]]:
    """Tier name -> provider ids (order = fallback). Built-ins derive from the configured providers; config overrides.
    Unknown ids in a configured tier are dropped with a warning."""
    s = config.load()
    enabled = [p for p in s.routing if p.enabled]
    out = {"default": [p.id for p in enabled],
           "strong": [p.id for p in enabled if p.trust != "local"],
           "local": [p.id for p in enabled if p.trust == "local"]}
    known = {p.id for p in s.routing}
    for name, ids in s.router.tiers.items():
        good = [i for i in ids if i in known]
        bad = [i for i in ids if i not in known]
        if bad and f"{name}:{bad}" not in _warned:
            _warned.add(f"{name}:{bad}")
            log.warning("router.tiers.%s lists unknown providers: %s", name, ", ".join(bad))
        out[name] = good
    return out


# --- rules ---------------------------------------------------------------------------------------------------
def _prefix_hit(text: str, prefix: str) -> str | None:
    """The text after `prefix` when the request starts with it as a word (case-insensitive), else None."""
    t = text.lstrip()
    if not prefix or not t.lower().startswith(prefix.lower()):
        return None
    rest = t[len(prefix):]
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


def _match(rule: config.RouteRule, text: str, surface: str, has_image: bool) -> bool:
    """The non-prefix matchers of a rule (all that are given must hold)."""
    if rule.keywords and not any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", text, re.I) for k in rule.keywords if k):
        return False
    if rule.surface:
        surfaces = [rule.surface] if isinstance(rule.surface, str) else rule.surface
        if surface not in surfaces:
            return False
    if rule.min_chars and len(text) < rule.min_chars:
        return False
    if rule.has_image is not None and rule.has_image != has_image:
        return False
    return True


def _label(rule: config.RouteRule, i: int) -> str:
    return rule.name or rule.prefix or f"rule {i + 1}"


def surface_of(caller) -> str:
    if getattr(caller, "surface", ""):
        return caller.surface
    n = caller.name or ""
    if n.startswith("discord:"):
        return "discord"
    if n == "ask:cli":
        return "cli"
    if n.startswith("ask:"):
        return "rest"
    return "job"


@dataclass
class Plan:
    text: str  # the request with any routing prefix stripped
    surface: str
    interactive: bool
    thread_id: str | None
    order: list[str]  # provider ids, preferred first (before the candidate re-check)
    tier: str
    rule: str
    override: bool = False  # chosen by the user for this request (prefix, model id, channel setting)
    strict: bool = False  # an override: don't append the default order's providers behind the tier
    sticky: str | None = None
    sticky_tier: str = ""
    base_order: list[str] = field(default_factory=list)  # order without the sticky choice
    base_tier: str = ""
    base_rule: str = ""
    candidates: list[str] = field(default_factory=list)
    retry_of: int | None = None
    tools: list[str] = field(default_factory=list)
    tool_errors: int = 0
    tool_calls: int = 0
    track: bool = True
    stored: dict | None = None  # the thread's stored route: actual provider, planned provider/tier, fell_back, reason
    planned: str = ""  # the provider the plan wanted first (the actual one may differ after a fallback)
    planned_tier: str = ""
    switched_from: str = ""  # a switch back to the planned provider: the fallback provider it replaced
    fallback_reason: str = ""  # set when something other than a provider error caused the fallback (GPU busy)
    decision: str = ""  # stay / switch / GPU choice, with its reason: written to the routing log


def _tier_order(name: str, t: dict[str, list[str]], strict: bool) -> list[str]:
    ids = list(t.get(name, []))
    if not strict or not ids:
        ids += [i for i in t["default"] if i not in ids]
    return ids


def plan(caller, text: str, provider_order: list[str] | None = None, thread_id: str | None = None,
         has_image: bool = False) -> Plan:
    """Decide rule, tier and provider order for one request. Pure config + one DB read; no provider is probed."""
    s = config.load()
    t = tiers()
    surface = surface_of(caller)
    p = Plan(text=text, surface=surface, interactive=surface in INTERACTIVE, thread_id=thread_id, order=[], tier="default",
             rule="default")
    rules = [r for r in s.router.route_rules if r.tier in t or _warn_tier(r)]
    if provider_order:  # explicit: /v1 model id, or the channel's provider setting
        p.override, p.strict, p.rule = True, True, "model"
        p.tier = getattr(caller, "tier_hint", "") or "model"
        p.order = list(provider_order)
        p.base_order, p.base_tier, p.base_rule = p.order, p.tier, p.rule
        return p
    for i, r in enumerate(rules):
        if r.prefix:
            rest = _prefix_hit(text, r.prefix)
            if rest and _match(r, rest, surface, has_image):
                p.text, p.override, p.strict = rest, True, True
                p.tier, p.rule = r.tier, _label(r, i)
                p.order = _tier_order(r.tier, t, True)
                p.base_order, p.base_tier, p.base_rule = p.order, p.tier, p.rule
                p.retry_of = _previous_row(thread_id)
                return p
    p.tier, p.rule = "default", "default"
    for i, r in enumerate(rules):
        if not r.prefix and _match(r, text, surface, has_image):
            p.tier, p.rule = r.tier, _label(r, i)
            break
    p.order = _tier_order(p.tier, t, False)
    p.base_order, p.base_tier, p.base_rule = p.order, p.tier, p.rule
    if s.router.sticky and thread_id:
        _init()
        r = db.q("SELECT provider, tier, planned_provider, planned_tier, fell_back, reason FROM thread_route "
                 "WHERE thread_id=?", thread_id)
        if r:
            p.sticky, p.sticky_tier, p.stored = r[0]["provider"], r[0]["tier"], r[0]
    return p


def _warn_tier(r: config.RouteRule) -> bool:
    key = f"rule:{r.tier}"
    if key not in _warned:
        _warned.add(key)
        log.warning("route rule %r points at unknown tier %r; rule ignored", r.name or r.prefix, r.tier)
    return False


def _previous_row(thread_id: str | None) -> int | None:
    if not thread_id:
        return None
    _init()
    r = db.q("SELECT id FROM routing WHERE thread_id=? ORDER BY id DESC LIMIT 1", thread_id)
    return r[0]["id"] if r else None


async def apply(p: Plan, need: router.Need) -> None:
    """Set need.order from the plan, never widening what router.candidates() allows for this need (the re-check).
    A thread whose last turn fell back keeps the fallback provider only when the thread is "hard"; otherwise it goes
    back to the planned provider once that is available again (decided 2026-11-09, docs/DECISIONS.md)."""
    base = router.Need(capabilities=set(need.capabilities), private=need.private, min_context=need.min_context)
    allowed = [c.id for c in await router.candidates(base)]
    p.candidates = allowed
    ok = set(allowed)
    order = list(p.order)
    p.planned = p.planned or (p.base_order[0] if p.base_order else "")
    p.planned_tier = p.planned_tier or p.base_tier
    st = p.stored
    if p.sticky and p.sticky in ok and not p.override:
        order = [p.sticky] + [i for i in router_tier_order(p.sticky_tier or p.base_tier, p) if i != p.sticky]
        p.tier, p.rule = p.sticky_tier or p.base_tier, "sticky"
        p.planned, p.planned_tier = p.sticky, p.sticky_tier or p.base_tier  # a plain sticky choice is its own plan
        if st and st.get("fell_back") and st.get("planned_provider") and st["planned_provider"] != p.sticky:
            planned = st["planned_provider"]
            p.planned, p.planned_tier = planned, st.get("planned_tier") or p.base_tier
            if planned not in ok or not await _ready(planned):
                p.sticky = None  # the planned provider is still not there: plan from scratch, it may fall back again
                order, p.tier, p.rule = list(p.order), p.base_tier, p.base_rule
                p.decision = f"replan: {planned} still not available"
            else:
                hard, why = hard_thread(p, st, planned)
                if hard:
                    p.decision = f"stay on {p.sticky}: {why}"
                else:
                    p.switched_from, p.decision = p.sticky, f"switch back to {planned} from {p.sticky}: not a hard thread"
                    p.sticky = None
                    order = [planned] + [i for i in router_tier_order(p.planned_tier, p) if i != planned]
                    p.tier, p.rule = p.planned_tier, "switched back"
    else:
        p.sticky = None
    final = [i for i in order if i in ok]
    if not final:  # nothing of the chosen tier is allowed (e.g. a private thread asked for /strong): default order
        final = [i for i in allowed]
        p.rule += " (no candidate)"
    p.order = final
    need.order = final
    need.interactive = p.interactive


async def _ready(provider_id: str) -> bool:
    """The planned provider can answer right now (a local host that is up with its model loaded, GPU not guarded off)."""
    prov = next((x for x in config.load().routing if x.id == provider_id), None)
    if prov is None:
        return False
    try:
        return bool(await router.healthy(prov, start=False))
    except Exception:
        return False


def hard_thread(p: Plan, st: dict, planned: str) -> tuple[bool, str]:
    """Is this thread worth keeping on the fallback provider? (switching would cost context and quality)"""
    r = config.load().router
    if (st.get("planned_tier") or "") == "strong":
        return True, "the plan's tier was strong"
    row = db.q("SELECT tool_calls, turns FROM routing WHERE thread_id=? ORDER BY id DESC LIMIT 1", p.thread_id)
    if row and (row[0]["tool_calls"] or 0) >= r.stay_tool_calls:
        return True, f"the last run made {row[0]['tool_calls']} tool calls"
    if p.thread_id and db.q("SELECT 1 FROM thread_items WHERE thread_id=? AND kind='summary' LIMIT 1", p.thread_id):
        return True, "the history was already condensed"
    prov = next((x for x in config.load().routing if x.id == planned), None)
    if prov is not None and p.thread_id:
        chars = db.q("SELECT COALESCE(SUM(LENGTH(text)),0) AS n FROM thread_items WHERE thread_id=? AND kind IN "
                     "('request','answer','summary','research')", p.thread_id)[0]["n"]
        if chars / 4 > r.stay_history_share * prov.context_tokens:
            return True, f"the history (~{int(chars / 4)} tokens) is over {int(r.stay_history_share * 100)}% of {planned}'s context"
    return False, "easy"


def router_tier_order(tier: str, p: Plan) -> list[str]:
    t = tiers()
    return _tier_order(tier, t, False) if tier in t else list(p.base_order)


def track(caller, p: Plan) -> None:
    """Record tool names and errors of this run through the caller's on_tool hook (kept, not replaced)."""
    prev = caller.on_tool

    async def hook(name, args, status):
        p.tool_calls += 1
        if name not in p.tools:
            p.tools.append(name)
        if status in ("error", "denied"):
            p.tool_errors += 1
        if prev:
            await prev(name, args, status)
    caller.on_tool = hook


def prefix_for(tier: str) -> str:
    """The route-rule prefix that selects `tier` (e.g. "/local"), or "" when no rule has one."""
    return next((r.prefix for r in config.load().router.route_rules if r.prefix and r.tier == tier), "")


def finish(p: Plan, res, thread_id: str, error: str = "") -> dict:
    """Log the decision, remember the thread's route (planned and actual provider), and return the routing facts for
    footers. A fallback is remembered as a fallback, so the next turn can decide to stay or switch back."""
    _init()
    provider = getattr(res, "provider", "") if res is not None else ""
    cost = float(getattr(res, "cost_usd", 0) or 0) if res is not None else 0.0
    fell = getattr(res, "fell_back", []) or []
    planned = provider if p.override else (p.planned or (p.base_order[0] if p.base_order else provider))
    fell_back = bool(provider and planned and provider != planned)
    reason = ""
    if fell_back:
        if p.decision.startswith("stay on"):
            reason = (p.stored or {}).get("reason") or "not ready"  # kept on the fallback provider: same reason as before
        elif p.fallback_reason:
            reason = p.fallback_reason
        elif fell:
            reason = fell[0].split(": ", 1)[-1][:80] if ": " in fell[0] else fell[0][:80]
        elif planned not in p.candidates:
            reason = "not allowed here"
        else:
            t = router._starting.get(planned)
            reason = "starting" if t is not None and not t.done() else "not ready"
    pl_tier = p.planned_tier or p.base_tier or p.tier
    waited = float(getattr(res, "waited_s", 0) or 0) if res is not None else 0.0
    if waited >= 0.5:
        p.decision = (p.decision + "; " if p.decision else "") + f"waited {waited:.0f}s for local start"
    try:
        db.x("INSERT INTO routing(ts,thread_id,surface,rule,tier,candidates,provider,fallbacks,turns,tool_errors,tools,"
             "cost_usd,retry_of,override,error,tool_calls,decision) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             time.time(), thread_id, p.surface, p.rule, p.tier, json.dumps(p.candidates), provider,
             json.dumps(fell), getattr(res, "turns", 0) if res is not None else 0,
             p.tool_errors, json.dumps(p.tools), cost, p.retry_of, int(p.override), error[:300], p.tool_calls,
             p.decision[:300])
        private = False
        try:
            from . import memory
            private = bool(thread_id and memory.thread_exists(thread_id) and memory.thread_private(thread_id))
        except Exception:
            pass
        local = {x.id for x in config.load().routing if x.trust == "local"}
        if provider and config.load().router.sticky and not (private and provider not in local):
            db.x("INSERT INTO thread_route(thread_id,provider,tier,ts,planned_provider,planned_tier,fell_back,reason) "
                 "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET provider=excluded.provider, "
                 "tier=excluded.tier, ts=excluded.ts, planned_provider=excluded.planned_provider, "
                 "planned_tier=excluded.planned_tier, fell_back=excluded.fell_back, reason=excluded.reason",
                 thread_id, provider, p.tier, time.time(), planned, pl_tier, int(fell_back), reason)
    except Exception:
        log.warning("could not write the routing log", exc_info=True)
    note = pivot = ""
    if fell_back:
        note = f"fell back from {planned}: {reason}"
        pf = prefix_for("local" if any(x.id == planned and x.trust == "local" for x in config.load().routing) else "strong")
        pivot = f"reply {pf} to switch back" if pf else ""
    elif p.switched_from and provider == planned:
        note = f"switched back from {p.switched_from}"
        pf = prefix_for("strong")
        pivot = f"{pf} to stay on the cloud" if pf else ""
    return {"rule": p.rule, "tier": p.tier, "tools": list(p.tools), "tool_errors": p.tool_errors, "override": p.override,
            "sticky": bool(p.sticky), "retry_of": p.retry_of, "note": note, "pivot": pivot, "decision": p.decision,
            "waited": round(waited)}


# --- footer --------------------------------------------------------------------------------------------------
def footer_enabled(surface: str) -> bool:
    r = config.load().router
    return r.footer and surface in r.footer_surfaces


def footer_line(provider: str, info: dict | None, cost: float) -> str:
    info = info or {}
    bits = [provider + (f" ({info['note']})" if info.get("note") else ""),
            f"rule: {info['rule']}" if info.get("rule") else ""]
    if info.get("waited"):
        bits.append(f"waited {info['waited']}s for local start")
    if info.get("tools"):
        bits.append("tools: " + ", ".join(info["tools"][:6]))
    bits.append(f"${cost:.3f}")
    if info.get("pivot"):
        bits.append(info["pivot"])
    return f"{FOOTER_MARK} " + " · ".join(b for b in bits if b)


def strip_footer(text: str) -> str:
    """Remove a trailing routing footer (the model must never see its own footers in the history)."""
    return FOOTER_RE.sub("", text or "").rstrip() if FOOTER_MARK in (text or "") else text


# --- summary (pmjc routing --days N, dashboard) -----------------------------------------------------------
def summary(days: float = 7) -> dict:
    _init()
    rows = db.q("SELECT * FROM routing WHERE ts>=? ORDER BY id", time.time() - days * 86400)
    by: dict[str, dict] = {}
    for r in rows:
        k = f"{r['rule']} -> {r['provider'] or '(none)'}"
        d = by.setdefault(k, {"rule": r["rule"], "provider": r["provider"], "tier": r["tier"], "n": 0, "turns": 0,
                              "tool_errors": 0, "cost_usd": 0.0, "retries": 0})
        d["n"] += 1
        d["turns"] += r["turns"] or 0
        d["tool_errors"] += r["tool_errors"] or 0
        d["cost_usd"] = round(d["cost_usd"] + (r["cost_usd"] or 0), 5)
        d["retries"] += 1 if r["retry_of"] else 0
    retried = {r["retry_of"] for r in rows if r["retry_of"]}
    return {"days": days, "requests": len(rows), "retries": sum(1 for r in rows if r["retry_of"]),
            "misrouted_rows": sorted(retried), "groups": sorted(by.values(), key=lambda d: -d["n"])}
