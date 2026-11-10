"""Run grants (roadmap phase8/m; design in DECISIONS.md "Run grants and per-tool permission levels").

One owner approval covers a batch of act calls that match machine-checked rules.

- A rule is `{"tool": name, "args": {arg: constraint}}`. Constraints: closed `{"eq": v}`, `{"in": [...]}`,
  `{"range": [lo, hi]}`, `{"template": "git -C <path> push origin <word>"}`; open `{"re": "..."}`, `{"any": true}`.
  Arguments a rule doesn't name must be absent from the call or equal the tool's default.
- Command arguments (`command`, `script`, `cmd`) accept only eq, in and template: a regex over a shell string is not a
  constraint. Template slots have fixed character classes without shell metacharacters, quotes or whitespace.
- Run state: clean < derived (used a pmjc_research result) < untrusted. A grant covers runs up to its `cover`; open
  rules cover clean runs only. A tainted run may only request closed rules.
- Session grants are bound to `Caller.session_key` (the object that holds the taint bits), expire and have a call cap.
  Standing grants come from the owner's settings.yaml (`grants:`), apply to every agent caller and have neither.
- The executor checks grants only inside its act block, after the egress and laundering guards (see executor._call_tool).
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from . import config, db, defang, levels, registry

log = logging.getLogger("pmjc.grants")

TOOL = "pmjc_grant"  # internal approval tool: not registered, no model can call it; _decide activates the grant
REQUEST_TOOL = "pmjc_grant_request"
RESEARCH_TOOL = "pmjc_research"  # its result makes the run "derived"
STATES = ("clean", "derived", "untrusted")
COMMAND_ARGS = {"command", "script", "cmd"}
CLOSED = ("eq", "in", "range", "template")
OPEN = ("re", "any")
MAX_RULES, MAX_IN, MAX_RE, MAX_VALUE, MAX_TEMPLATE, MAX_RE_INPUT = 20, 50, 200, 2000, 500, 10000
MINUTES, CALLS = (1, 240), (1, 200)
SLOTS = {"word": r"[A-Za-z0-9_.@:+-]{1,128}", "path": r"[A-Za-z0-9_./\\:~+-]{1,260}", "int": r"[0-9]{1,9}"}
_SLOT = re.compile(r"<(?:([A-Za-z_][A-Za-z0-9_]{0,31}):)?(word|path|int)>")
_PROBES = ("x", "a b", "; rm -rf /", "$(id)", "0")  # a pattern that fully matches all of these matches (nearly) anything
_REQUIRED = object()

class GrantError(ValueError):
    pass


# --- run state ---------------------------------------------------------------------------------------------
def run_state(caller) -> str:
    if getattr(caller, "untrusted", False):
        return "untrusted"
    return "derived" if getattr(caller, "derived", False) else "clean"


def _rank(state: str) -> int:
    return STATES.index(state) if state in STATES else len(STATES)


# --- templates -----------------------------------------------------------------------------------------------
def compile_template(tpl: str) -> tuple[re.Pattern, list[str]]:
    """Anchored regex for a template (literals escaped, slots as named groups) and the class of each slot."""
    parts, kinds, pos = [], [], 0
    for m in _SLOT.finditer(tpl):
        parts.append(re.escape(tpl[pos:m.start()]))
        parts.append(f"(?P<s{len(kinds)}>{SLOTS[m.group(2)]})")
        kinds.append(m.group(2))
        pos = m.end()
    parts.append(re.escape(tpl[pos:]))
    return re.compile("".join(parts)), kinds


def template_match(tpl: str, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    rx, kinds = compile_template(tpl)
    m = rx.fullmatch(value)
    if not m:
        return False
    for i, k in enumerate(kinds):
        s = m.group(f"s{i}")
        if s.startswith("-"):  # never an option: `git push origin <word>` must not become `... --force`
            return False
        if k == "path" and ".." in re.split(r"[/\\]", s):  # a path slot never holds a `..` segment
            return False
    return True


# --- tool parameters -----------------------------------------------------------------------------------------
def params(t: registry.ToolSpec) -> dict[str, Any]:
    """Argument name -> default (or _REQUIRED) from the tool's schema or signature."""
    if t.schema is not None:
        props = (t.schema or {}).get("properties") or {}
        return {k: (v.get("default", _REQUIRED) if isinstance(v, dict) else _REQUIRED) for k, v in props.items()}
    out = {}
    for p in inspect.signature(t.fn).parameters.values():
        if p.name in ("self",) or p.name.startswith("_") or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        out[p.name] = _REQUIRED if p.default is inspect.Parameter.empty else p.default
    return out


# --- rules -----------------------------------------------------------------------------------------------------
def _same(a: Any, b: Any) -> bool:
    return (isinstance(a, bool) == isinstance(b, bool)) and a == b


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _short(v: Any) -> bool:
    return len(json.dumps(v, default=str)) <= MAX_VALUE


def kind(c: dict) -> str:
    return "closed" if next(iter(c)) in CLOSED else "open"


def rule_closed(rule: dict) -> bool:
    return all(kind(c) == "closed" for c in rule["args"].values())


def rule_single(rule: dict) -> bool:
    """Every named argument is fixed to one value (eq, a one-value in, a template without slots)."""
    for c in rule["args"].values():
        k, v = next(iter(c.items()))
        if not (k == "eq" or (k == "in" and len(v) == 1) or (k == "template" and not _SLOT.search(v))):
            return False
    return True


def _check_constraint(tool: str, arg: str, c: Any) -> dict:
    where = f"{tool}.{arg}"
    if not isinstance(c, dict) or len(c) != 1:
        raise GrantError(f"{where}: a constraint is one of eq, in, range, template, re, any (e.g. {{\"eq\": \"x\"}})")
    k, v = next(iter(c.items()))
    if k in OPEN and arg in COMMAND_ARGS:
        raise GrantError(f"{where}: a command argument accepts only eq, in or template (a regex or `any` over a shell "
                         "string is not a constraint); use a template such as \"systemctl status <word>\"")
    if k == "eq":
        if not _short(v):
            raise GrantError(f"{where}: eq value too long")
    elif k == "in":
        if not isinstance(v, list) or not 1 <= len(v) <= MAX_IN or not all(_short(x) for x in v):
            raise GrantError(f"{where}: in takes a list of 1..{MAX_IN} values")
    elif k == "range":
        if not (isinstance(v, list) and len(v) == 2 and all(_num(x) for x in v) and v[0] <= v[1]):
            raise GrantError(f"{where}: range takes [low, high] numbers")
    elif k == "template":
        if not isinstance(v, str) or not v or len(v) > MAX_TEMPLATE:
            raise GrantError(f"{where}: template must be a non-empty string up to {MAX_TEMPLATE} characters")
        if len(_SLOT.findall(v)) > 20:
            raise GrantError(f"{where}: at most 20 slots per template")
    elif k == "re":
        if not isinstance(v, str) or not v or len(v) > MAX_RE:
            raise GrantError(f"{where}: re must be a pattern of 1..{MAX_RE} characters")
        try:
            rx = re.compile(v)
        except re.error as e:
            raise GrantError(f"{where}: bad pattern ({e})") from None
        if all(rx.fullmatch(p) for p in _PROBES):
            raise GrantError(f"{where}: the pattern matches anything; name the values (in) or use a narrower pattern")
    elif k == "any":
        if v is not True:
            raise GrantError(f"{where}: write any as {{\"any\": true}}")
    else:
        raise GrantError(f"{where}: unknown constraint {k!r}")
    return {k: v}


def validate(rules: Any, state: str = "clean", cover_tainted: bool = False, auto_approve=()) -> tuple[list[dict], str]:
    """Normalised rules and the cover state, or GrantError. `state` is the requesting run's state."""
    if not isinstance(rules, list) or not 1 <= len(rules) <= MAX_RULES:
        raise GrantError(f"rules must be a list of 1..{MAX_RULES} rules")
    s = config.load()
    reg = registry.get()
    out = []
    for r in rules:
        if not isinstance(r, dict) or not isinstance(r.get("tool"), str) or set(r) - {"tool", "args"}:
            raise GrantError('each rule is {"tool": name, "args": {arg: constraint}}')
        name, args = r["tool"], r.get("args") or {}
        if not isinstance(args, dict):
            raise GrantError(f"{name}: args must be an object")
        t = reg.tool(name)
        if t is None:
            raise GrantError(f"unknown tool {name}")
        if t.scope != "act":
            raise GrantError(f"{name} is not an action tool; it needs no grant")
        lvl = levels.effective(name, t.level, s.tool_levels, set(s.auto_approve) | set(auto_approve))
        if levels.hard_floor(name) or lvl not in ("ask", "auto_clean"):
            raise GrantError(f"{name} can't be covered by a grant (level {lvl}): every call needs its own approval")
        known = params(t)
        norm = {}
        for arg, c in args.items():
            if arg not in known:
                raise GrantError(f"{name} has no argument {arg!r}")
            norm[arg] = _check_constraint(name, arg, c)
        out.append({"tool": name, "args": norm})
    closed = all(rule_closed(r) for r in out)
    if state != "clean" and not closed:
        raise GrantError("this run has read outside content, so it may only request closed rules (eq, in, range, template)")
    if cover_tainted and not closed:
        raise GrantError("cover_tainted needs every rule to be closed (eq, in, range, template)")
    return out, ("untrusted" if cover_tainted else "clean")


def _value_ok(c: dict, v: Any) -> bool:
    k, x = next(iter(c.items()))
    if k == "eq":
        return _same(v, x)
    if k == "in":
        return any(_same(v, y) for y in x)
    if k == "range":
        return _num(v) and x[0] <= v <= x[1]
    if k == "template":
        return template_match(x, v)
    if k == "re":
        return isinstance(v, str) and len(v) <= MAX_RE_INPUT and re.fullmatch(x, v) is not None
    return k == "any"


def rule_matches(rule: dict, name: str, args: dict, known: dict[str, Any]) -> bool:
    if rule["tool"] != name:
        return False
    for arg, v in args.items():
        if arg not in rule["args"] and not (arg in known and known[arg] is not _REQUIRED and _same(v, known[arg])):
            return False
    for arg, c in rule["args"].items():
        if arg in args:
            v = args[arg]
        elif known.get(arg, _REQUIRED) is not _REQUIRED:
            v = known[arg]
        else:
            return False
        if not _value_ok(c, v):
            return False
    return True


# --- summary and approval views -----------------------------------------------------------------------------
def _constraint_text(c: dict) -> str:
    k, v = next(iter(c.items()))
    if k == "eq":
        return f"= {json.dumps(v, default=str)}"
    if k == "in":
        return "one of " + ", ".join(json.dumps(x, default=str) for x in v)
    if k == "range":
        return f"from {v[0]} to {v[1]}"
    if k == "template":
        return f"matches template {json.dumps(v)} (slots: <word> letters/digits/._@:+-, <path> no spaces or .., <int> digits; never starting with -)"
    if k == "re":
        return f"matches regex {json.dumps(v)}"
    return "ANY value"


def rule_lines(rules: list[dict]) -> list[dict]:
    out = []
    for r in rules:
        badge = "CLOSED" if rule_closed(r) else "OPEN"
        args = "; ".join(f"{a} {_constraint_text(c)}" for a, c in r["args"].items()) or "no arguments (defaults only)"
        out.append({"path": r["tool"], "text": f"[{badge}] {args}; other arguments at their defaults", "badge": badge})
    return out


def summary(rules: list[dict], minutes: int | None, max_calls: int | None, cover: str) -> str:
    """The human text of a grant, built from the rules only (never from the model's words)."""
    lines = ["Run grant: these calls run without asking again:"]
    lines += [f"- {ln['path']} {ln['text']}" for ln in rule_lines(rules)]
    if minutes is not None:
        lines.append(f"For this session only; expires {minutes} minutes after approval; at most {max_calls} calls.")
    else:
        lines.append("Standing grant: no expiry, no call cap.")
    if _rank(cover) > 0:
        lines.append("ALSO after the run read outside content (messages, web pages, command output).")
    return "\n".join(lines)


def intent_text(intent: str) -> str:
    return defang.text((intent or "").strip()[:500], True, wrap=False)


def view(a: dict) -> dict | None:
    """Same shape as settings_edit.view for a pmjc_grant approval (plain strings; each medium escapes)."""
    if a.get("tool") != TOOL:
        return None
    try:
        args = json.loads(a.get("args") or "{}")
    except ValueError:
        return None
    rules = args.get("rules") or []
    lines = rule_lines(rules)
    badge = "OPEN" if any(ln["badge"] == "OPEN" for ln in lines) else "CLOSED"
    if _rank(args.get("cover", "clean")) > 0:
        badge += " +TAINTED"
    title = f"Run grant: {len(rules)} rule(s), {args.get('minutes')} min, max {args.get('max_calls')} calls"
    return {"title": title, "badge": badge, "two_step": False, "surface": "", "reason": args.get("intent") or "",
            "summary": args.get("summary") or "", "lines": [{"path": ln["path"], "text": ln["text"]} for ln in lines]}


def api_view(a: dict) -> dict | None:
    v = view(a)
    return None if v is None else {k: v[k] for k in ("title", "badge", "two_step", "surface", "reason", "lines")}


def plain_text(a: dict) -> str:
    v = view(a)
    return v["summary"] + (f'\nThe model says: "{v["reason"]}"' if v["reason"] else "")


def html_view(a: dict) -> str:
    import html as h
    v = view(a)
    said = f'<p><i>The model says:</i> &ldquo;{h.escape(v["reason"])}&rdquo;</p>' if v["reason"] else ""
    return (f'<p><b>[{h.escape(v["badge"])}]</b> {h.escape(v["title"])}</p>'
            f'<pre style="white-space:pre-wrap;background:#eee;padding:1em">{h.escape(v["summary"])}</pre>{said}')


def discord_text(a: dict) -> str:
    from .settings_edit import _md
    v = view(a)
    body = v["summary"].replace("```", "'''").replace("@", "＠")
    said = f'\nThe model says: "{_md(v["reason"])}"' if v["reason"] else ""
    return f"**Approval needed: [{v['badge']}] {_md(v['title'])}**\n```\n{body}\n```{said}"


# --- storage and lifecycle ---------------------------------------------------------------------------------
async def request(caller, intent: str, rules: Any, minutes: int = 30, max_calls: int = 50,
                  cover_tainted: bool = False) -> dict:
    """Validate, store a pending grant and file ONE approval for it (the internal tool pmjc_grant)."""
    from . import executor
    if caller is None:
        raise GrantError("no caller")
    try:
        minutes, max_calls = int(minutes), int(max_calls)
    except (TypeError, ValueError):
        raise GrantError("minutes and max_calls must be whole numbers") from None
    if not MINUTES[0] <= minutes <= MINUTES[1]:
        raise GrantError(f"minutes must be {MINUTES[0]}..{MINUTES[1]}")
    if not CALLS[0] <= max_calls <= CALLS[1]:
        raise GrantError(f"max_calls must be {CALLS[0]}..{CALLS[1]}")
    state = run_state(caller)
    norm, cover = validate(rules, state, bool(cover_tainted), caller.auto_approve)
    gid = "g_" + secrets.token_urlsafe(9)
    text = summary(norm, minutes, max_calls, cover)
    said = intent_text(intent)
    now = time.time()
    db.x("INSERT INTO grants(id,session_key,caller_name,thread_id,rules,intent,summary,cover,max_calls,calls_used,minutes,"
         "expires,status,created,created_state,source) VALUES(?,?,?,?,?,?,?,?,?,0,?,0,'pending',?,?,'session')",
         gid, caller.session_key, caller.name, caller.thread_id, json.dumps(norm), said, text, cover, max_calls, minutes,
         now, state)
    args = {"grant_id": gid, "rules": norm, "minutes": minutes, "max_calls": max_calls, "cover": cover,
            "summary": text, "intent": said}
    res = await executor.request_approval(caller, TOOL, args, f"run grant: {len(norm)} rule(s) for {minutes} min")
    db.x("UPDATE grants SET approval_id=? WHERE id=?", res.get("approval_id"), gid)
    return {**res, "grant_id": gid, "summary": text}


def activate(args: dict) -> dict:
    """The owner approved: the grant becomes active with exactly the approved (hash-checked) rules, cap and cover."""
    gid = args.get("grant_id")
    expires = time.time() + int(args["minutes"]) * 60
    n = db.x("UPDATE grants SET status='active', expires=?, rules=?, max_calls=?, cover=? WHERE id=? AND status='pending'",
             expires, json.dumps(args["rules"]), int(args["max_calls"]), args["cover"], gid)
    if not n:
        raise GrantError(f"grant {gid} is no longer pending")
    db.audit("owner", TOOL, "", "grant_active", gid)
    return {"grant_id": gid, "status": "active", "expires": expires, "max_calls": int(args["max_calls"]),
            "note": "Calls that match the rules now run without asking; others still need approval."}


def mark_denied(args: dict) -> None:
    db.x("UPDATE grants SET status='denied' WHERE id=? AND status='pending'", args.get("grant_id"))


def _expire_stale() -> None:
    now = time.time()
    db.x("UPDATE grants SET status='expired' WHERE status='active' AND expires<=?", now)
    ttl = config.load().approval_ttl_seconds
    db.x("UPDATE grants SET status='expired' WHERE status='pending' AND created<?", now - ttl - 60)


def list_grants(status: str | None = None) -> list[dict]:
    _expire_stale()
    rows = db.q("SELECT id,caller_name,thread_id,rules,intent,summary,cover,max_calls,calls_used,expires,status,"
                "approval_id,created,created_state,source FROM grants WHERE ?='all' OR status=? OR (? IS NULL AND "
                "status IN ('pending','active')) ORDER BY created DESC LIMIT 500", status or "", status or "", status)
    for r in rows:
        r["rules"] = json.loads(r["rules"] or "[]")
    standing = [{"id": g["id"], "source": "standing", "status": "active", "cover": g["cover"], "rules": g["rules"],
                 "summary": summary(g["rules"], None, None, g["cover"])} for g in standing_grants()]
    return standing + rows


def revoke(gid: str | None = None) -> int:
    """Revoke one session grant, or every pending/active one (gid None: the kill switch)."""
    if gid:
        n = db.x("UPDATE grants SET status='revoked' WHERE id=? AND status IN ('pending','active')", gid)
    else:
        n = db.x("UPDATE grants SET status='revoked' WHERE status IN ('pending','active')")
    db.audit("owner", TOOL, "", "grant_revoked", gid or f"all ({n})")
    return n


# --- standing grants (owner-written settings.yaml `grants:`) ---------------------------------------------------
_bad_logged: set[str] = set()


def standing_grants() -> list[dict]:
    """Valid standing grants as {id, cover, rules}; an invalid one is logged once and skipped."""
    out = []
    for i, g in enumerate(config.load().grants):
        gid = f"standing:{g.name or i}"
        try:
            if g.cover not in ("clean", "untrusted"):
                raise GrantError("cover must be clean or untrusted")
            rules, cover = validate(g.rules, "clean", g.cover == "untrusted")
            out.append({"id": gid, "cover": cover, "rules": rules})
        except GrantError as e:
            key = f"{gid}:{e}"
            if key not in _bad_logged:
                _bad_logged.add(key)
                log.warning("settings grants: %s is invalid and ignored: %s", gid, e)
    return out


# --- enforcement (executor._call_tool) -----------------------------------------------------------------------
@dataclass
class Match:
    id: str
    single: bool
    standing: bool


def find(caller, t: registry.ToolSpec, args: dict, need_single: bool = False) -> list[Match]:
    """Grants that cover this call for this caller in its current run state (session grants first)."""
    state = _rank(run_state(caller))
    known = params(t)
    out = []
    rows = db.q("SELECT id, rules, cover FROM grants WHERE session_key=? AND status='active' AND expires>? "
                "AND calls_used<max_calls", getattr(caller, "session_key", "") or "-", time.time())
    cands = [(r["id"], r["cover"], json.loads(r["rules"] or "[]"), False) for r in rows]
    try:
        cands += [(g["id"], g["cover"], g["rules"], True) for g in standing_grants()]
    except Exception:
        log.warning("standing grants could not be read", exc_info=True)
    for gid, cover, rules, standing in cands:
        if state > _rank(cover):
            continue
        for r in rules:
            if not isinstance(r, dict) or not rule_matches(r, t.name, args, known):
                continue
            if state > 0 and not rule_closed(r):  # defence in depth: an open rule never covers a tainted run
                continue
            single = rule_single(r)
            if need_single and not single:
                continue
            out.append(Match(gid, single, standing))
            break
    return out


def claim(m: Match) -> bool:
    """Count one call against a session grant (atomic; fails when revoked, expired or at the cap meanwhile)."""
    if m.standing:
        return True
    return bool(db.x("UPDATE grants SET calls_used=calls_used+1 WHERE id=? AND status='active' AND calls_used<max_calls "
                     "AND expires>?", m.id, time.time()))
