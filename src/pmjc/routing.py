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
  error TEXT);
CREATE INDEX IF NOT EXISTS routing_thread ON routing(thread_id, ts);
CREATE TABLE IF NOT EXISTS thread_route (thread_id TEXT PRIMARY KEY, provider TEXT, tier TEXT, ts REAL);
"""
INTERACTIVE = {"discord", "rest", "cli", "v1"}
FOOTER_MARK = "⟡"  # a glyph that does not occur in normal prose; the footer line starts with it
FOOTER_RE = re.compile(r"(?:\n[ \t]*)*(?:-# )?" + FOOTER_MARK + r" [^\n]*\Z")
_warned: set[str] = set()


def _init() -> None:
    c = db.conn()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='routing'").fetchone():
        c.executescript(DDL)


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
    track: bool = True


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
        r = db.q("SELECT provider, tier FROM thread_route WHERE thread_id=?", thread_id)
        if r:
            p.sticky, p.sticky_tier = r[0]["provider"], r[0]["tier"]
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
    """Set need.order from the plan, never widening what router.candidates() allows for this need (the re-check)."""
    base = router.Need(capabilities=set(need.capabilities), private=need.private, min_context=need.min_context)
    allowed = [c.id for c in await router.candidates(base)]
    p.candidates = allowed
    ok = set(allowed)
    order = list(p.order)
    if p.sticky and p.sticky in ok and not p.override:
        order = [p.sticky] + [i for i in router_tier_order(p.sticky_tier or p.base_tier, p) if i != p.sticky]
        p.tier, p.rule = p.sticky_tier or p.base_tier, "sticky"
    else:
        p.sticky = None
    final = [i for i in order if i in ok]
    if not final:  # nothing of the chosen tier is allowed (e.g. a private thread asked for /strong): default order
        final = [i for i in allowed]
        p.rule += " (no candidate)"
    p.order = final
    need.order = final
    need.interactive = p.interactive


def router_tier_order(tier: str, p: Plan) -> list[str]:
    t = tiers()
    return _tier_order(tier, t, False) if tier in t else list(p.base_order)


def track(caller, p: Plan) -> None:
    """Record tool names and errors of this run through the caller's on_tool hook (kept, not replaced)."""
    prev = caller.on_tool

    async def hook(name, args, status):
        if name not in p.tools:
            p.tools.append(name)
        if status in ("error", "denied"):
            p.tool_errors += 1
        if prev:
            await prev(name, args, status)
    caller.on_tool = hook


def finish(p: Plan, res, thread_id: str, error: str = "") -> dict:
    """Log the decision, remember the sticky provider, and return the routing facts for footers."""
    _init()
    provider = getattr(res, "provider", "") if res is not None else ""
    cost = float(getattr(res, "cost_usd", 0) or 0) if res is not None else 0.0
    try:
        db.x("INSERT INTO routing(ts,thread_id,surface,rule,tier,candidates,provider,fallbacks,turns,tool_errors,tools,"
             "cost_usd,retry_of,override,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             time.time(), thread_id, p.surface, p.rule, p.tier, json.dumps(p.candidates), provider,
             json.dumps(getattr(res, "fell_back", []) or []), getattr(res, "turns", 0) if res is not None else 0,
             p.tool_errors, json.dumps(p.tools), cost, p.retry_of, int(p.override), error[:300])
        if provider and config.load().router.sticky and (p.override or not p.sticky):
            db.x("INSERT INTO thread_route(thread_id,provider,tier,ts) VALUES(?,?,?,?) ON CONFLICT(thread_id) DO UPDATE "
                 "SET provider=excluded.provider, tier=excluded.tier, ts=excluded.ts", thread_id, provider, p.tier, time.time())
    except Exception:
        log.warning("could not write the routing log", exc_info=True)
    return {"rule": p.rule, "tier": p.tier, "tools": list(p.tools), "tool_errors": p.tool_errors, "override": p.override,
            "sticky": bool(p.sticky), "retry_of": p.retry_of}


# --- footer --------------------------------------------------------------------------------------------------
def footer_enabled(surface: str) -> bool:
    r = config.load().router
    return r.footer and surface in r.footer_surfaces


def footer_line(provider: str, info: dict | None, cost: float) -> str:
    info = info or {}
    bits = [provider, f"rule: {info['rule']}" if info.get("rule") else ""]
    if info.get("tools"):
        bits.append("tools: " + ", ".join(info["tools"][:6]))
    bits.append(f"${cost:.3f}")
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
