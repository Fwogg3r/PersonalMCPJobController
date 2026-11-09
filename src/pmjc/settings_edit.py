"""Configuring pmjc from chat: a bounded, safe-direction allowlist, owner-authored profiles, and a careful writer.

Design (docs/DECISIONS.md, "Configuring pmjc from chat"):
- `pmjc_settings_set` may move only the paths in REGISTRY, only in the safe direction (off, lower, disable, upward
  retention) and inside explicit bounds. Everything in HARD_DENY is refused first, in code, whatever the registry says.
- Chat can never run these tools from a tainted run, a job, a watch or an approval follow-up; they always need the
  owner's approval, even for auto_approve (see executor).
- Applying is a compare-and-swap under a cross-process file lock: the sha256 of settings.yaml at proposal time is part
  of the approved arguments, the file is re-read under the lock and a mismatch refuses. Only the changed keys are
  patched (ruamel round-trip: comments kept, no defaults written), the whole model and some invariants are validated,
  a backup is kept, and the file is replaced atomically. config.load() reads the file on every call, so other
  processes see the change at once.
- Profiles are named overlays the owner writes in settings.yaml; they may touch allowlisted paths in either direction
  but never hard-denied ones.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import config, db
from .config import Settings

TOOL_SET = "pmjc_settings_set"
TOOL_REVERT = "pmjc_settings_revert"
TOOL_PROFILE = "pmjc_profile_switch"
TOOLS = {TOOL_SET, TOOL_REVERT, TOOL_PROFILE}

MAX_PATHS = 3
RATE_LIMIT_PER_HOUR = 3
COOLDOWN_SECONDS = 30 * 60
KEEP_BACKUPS = 20
OFF_LOWER, ROUTING, COST = "off-lower", "routing", "cost"
TWO_STEP_CLASSES = {ROUTING, COST}
RISK_ORDER = [OFF_LOWER, ROUTING, COST]


class SettingsDenied(Exception):
    """A proposal chat isn't allowed to make (refused before any approval)."""


class SettingsError(Exception):
    """An approved change that could not be applied (stale file, invalid result)."""


# --- hard deny (checked first, in code) ------------------------------------------------------------------------
HARD_DENY_TOP = {
    "auto_approve", "allowed_hosts", "host", "port", "public_url", "credential_backend", "oauth", "enabled_plugins",
    "mcp_servers", "connectors", "discord_door", "notify_channels", "monitoring_sources", "output_link_allowlist",
    "audit_retention_days", "approval_ttl_seconds", "approval_poll_seconds", "profiles",
}
HARD_DENY_PREFIX = ("shell_command_",)
HARD_DENY_EXACT = {
    "research.tools", "front_door.enabled", "front_door.rate_limit_per_minute", "front_door.max_body_bytes",
    "front_door.keepalive_seconds", "router.gpu_busy_processes",
}
HARD_DENY_PROVIDER_FIELDS = {
    "id", "kind", "base_url", "model", "credential", "start", "start_timeout", "trust", "capabilities", "context_tokens",
    "reserve_tokens", "price_in_per_mtok", "price_out_per_mtok", "extra", "load_model",
}
_PROVIDER_PATH = re.compile(r"^routing\[([A-Za-z0-9][A-Za-z0-9_-]{0,31})\]\.([a-z_]+)$")
_PLAIN_PATH = re.compile(r"^[a-z_]+(\.[a-z_]+)?$")


def hard_denied(path: str) -> bool:
    """True for anything that widens reach or spend or touches auth, egress targets, approvals, audit, notify channels
    or programs to run. Checked before the registry, so editing the registry can't open one of these."""
    p = str(path).strip()
    low = p.lower()
    top = re.split(r"[.\[]", low, 1)[0]
    if top in HARD_DENY_TOP or low.startswith(HARD_DENY_PREFIX) or low in HARD_DENY_EXACT:
        return True
    m = _PROVIDER_PATH.match(p)
    if m and m.group(2) in HARD_DENY_PROVIDER_FIELDS:
        return True
    if low.startswith("routing"):  # any other routing form (whole list, an entry, odd spelling)
        return not (m and m.group(2) in PROVIDER_SPECS)
    return False


# --- the allowlist ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    kind: str  # bool | int | float | enum | enumlist | connlist | budget
    risk: str
    direction: str = "any"  # any | down | up | to_false | to_true | add_only
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    note: str = ""


SURFACES = ("discord", "rest", "cli", "v1", "job")

SPECS: dict[str, Spec] = {
    "router.sticky": Spec("bool", ROUTING),
    "router.footer": Spec("bool", OFF_LOWER),
    "router.footer_surfaces": Spec("enumlist", OFF_LOWER, choices=SURFACES),
    "router.stay_tool_calls": Spec("int", ROUTING, lo=1, hi=20),
    "router.stay_history_share": Spec("float", ROUTING, lo=0.2, hi=0.9),
    "router.gpu_busy_action": Spec("enum", ROUTING, choices=("ask", "local")),
    "router.gpu_busy_jobs": Spec("enum", ROUTING, choices=("wait", "local")),
    "router.gpu_busy_backoff_seconds": Spec("float", ROUTING, lo=30, hi=3600),
    "router.gpu_busy_max_waits": Spec("int", ROUTING, lo=0, hi=20),
    "research.max_turns": Spec("int", COST, "down", lo=1, hi=8),
    "research.max_chars": Spec("int", COST, "down", lo=200),
    "front_door.stream_progress": Spec("bool", OFF_LOWER),
    "front_door.history_turns": Spec("int", COST, lo=0, hi=10),
    "commands.wait_seconds": Spec("int", OFF_LOWER, lo=2, hi=30),
    "approval_wait_seconds": Spec("int", OFF_LOWER, lo=10, hi=55),
    "approval_outcome_notify": Spec("bool", OFF_LOWER, "to_true"),
    "thread_retention_days": Spec("int", OFF_LOWER, "up", hi=365),
    "result_ttl_hours": Spec("float", OFF_LOWER, "down", lo=0.1),
    "monitoring_enabled": Spec("bool", OFF_LOWER, "to_false"),
    "scheduled_jobs_enabled": Spec("bool", OFF_LOWER, "to_false"),
    "auto_start_local": Spec("bool", ROUTING),
    "disabled_connectors": Spec("connlist", OFF_LOWER, "add_only"),
}
PROVIDER_SPECS: dict[str, Spec] = {
    "enabled": Spec("bool", ROUTING, "to_false"),
    "schema_mode": Spec("enum", COST, choices=("full", "compact")),
    "tool_mode": Spec("enum", COST, choices=("all", "select")),
    "select_budget_tokens": Spec("int", COST, "down", lo=2000),
    "max_result_chars": Spec("int", COST, "down", lo=1000),
    "condense_at": Spec("float", COST, lo=0.4, hi=0.9),
    "monthly_budget_usd": Spec("budget", COST, "down"),
}
# connectors chat may never disable: the approval path itself
PROTECTED_CONNECTORS = {"pmjc", "discord_door"}


def parse(path: str) -> tuple[str, str | None]:
    """(registry key, provider id or None). Raises SettingsDenied for a hard-denied, unknown or malformed path."""
    p = str(path).strip()
    if not p.isascii() or len(p) > 80:
        raise SettingsDenied(f"`{_safe(p, 40)}` isn't a settings path")
    if hard_denied(p):
        raise SettingsDenied(f"`{p}` can't be changed from chat; edit settings.yaml or use the dashboard")
    m = _PROVIDER_PATH.match(p)
    if m:
        return f"routing[*].{m.group(2)}", m.group(1)
    if _PLAIN_PATH.match(p) and p in SPECS:
        return p, None
    raise SettingsDenied(f"`{p}` isn't a setting chat can change; edit settings.yaml or use the dashboard")


def spec_for(key: str) -> Spec:
    return PROVIDER_SPECS[key.split(".", 1)[1]] if key.startswith("routing[*]") else SPECS[key]


def canon(path: str) -> str:
    return str(path).strip()


def _provider(s: Settings, pid: str):
    for p in s.routing:
        if p.id == pid:
            return p
    raise SettingsDenied(f"no provider `{pid}` (see pmjc_settings_get for provider ids)")


def current(s: Settings, path: str) -> Any:
    key, pid = parse(path)
    if pid is not None:
        return getattr(_provider(s, pid), key.split(".", 1)[1])
    node: Any = s
    for part in key.split("."):
        node = getattr(node, part)
    return node


def _effective_old(s: Settings, path: str, old: Any) -> Any:
    """The value a 'down only' comparison starts from when the setting is unset (None means a derived default)."""
    key, pid = parse(path)
    if old is not None or pid is None:
        return old
    p = _provider(s, pid)
    if key.endswith("select_budget_tokens"):
        return int(p.context_tokens * 0.3)
    if key.endswith("max_result_chars"):
        return config.result_cap(p)
    return old  # monthly_budget_usd None: unlimited


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _typed(spec: Spec, v: Any, path: str) -> Any:
    k = spec.kind
    ok = True
    if k == "bool":
        ok = isinstance(v, bool)
    elif k == "int":
        ok = isinstance(v, int) and not isinstance(v, bool)
    elif k in ("float", "budget"):
        ok = _num(v)
        if ok:
            v = float(v)
    elif k == "enum":
        ok = isinstance(v, str) and v in spec.choices
    elif k == "enumlist":
        ok = (isinstance(v, list) and len(v) == len(set(map(str, v))) and all(isinstance(i, str) and i in spec.choices for i in v))
    elif k == "connlist":
        ok = (isinstance(v, list) and len(v) <= 64 and len(v) == len(set(map(str, v)))
              and all(isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", i) for i in v))
    if not ok:
        want = {"bool": "true or false", "int": "a whole number", "float": "a number", "budget": "a number (never null)",
                "enum": "one of " + ", ".join(spec.choices), "enumlist": "a list drawn from " + ", ".join(spec.choices),
                "connlist": "a list of connector ids"}[k]
        raise SettingsDenied(f"`{path}` must be {want}")
    if spec.kind in ("int", "float", "budget"):
        if spec.lo is not None and v < spec.lo:
            raise SettingsDenied(f"`{path}` must be at least {spec.lo:g}")
        if spec.hi is not None and v > spec.hi:
            raise SettingsDenied(f"`{path}` must be at most {spec.hi:g}")
        if spec.kind == "budget" and v < 0:
            raise SettingsDenied(f"`{path}` can't be negative")
    return v


def _protected(s: Settings) -> set[str]:
    return PROTECTED_CONNECTORS | {str(c) for c in s.notify_channels} | ({"discord"} if s.discord_door.enabled else set())


def _check_value(s: Settings, path: str, new: Any, owner: bool) -> tuple[Any, Any, Spec]:
    """Validate one (path, new value) against the current settings. owner=True (profiles, reverts) skips the direction
    rule because the owner authored the value, but type, bounds, hard-denies and the budget rules stay."""
    key, pid = parse(path)
    spec = spec_for(key)
    old = current(s, path)
    new = _typed(spec, new, path)
    if spec.kind == "connlist":
        added = set(new) - set(old)
        removed = set(old) - set(new)
        bad = sorted(added & _protected(s))
        if bad:
            raise SettingsDenied(f"`{path}`: {', '.join(bad)} can't be disabled from chat (approvals depend on it)")
        if removed and not owner:
            raise SettingsDenied(f"`{path}`: chat may only add to this list; re-enabling a connector is done in the dashboard")
        known = _known_connectors()
        unknown = sorted(i for i in added if known and i not in known)
        if unknown:
            raise SettingsDenied(f"`{path}`: unknown connector {', '.join(unknown)}")
        return old, new, spec
    if spec.kind == "enumlist":
        return old, new, spec
    eff = _effective_old(s, path, old)
    if not owner:
        d = spec.direction
        if d == "down" and eff is not None and new > eff:
            raise SettingsDenied(f"`{path}` may only be lowered from chat (now {eff:g}); raise it in the dashboard")
        if d == "up" and new < eff:
            raise SettingsDenied(f"`{path}` may only be raised from chat (now {eff:g})")
        if d == "to_false" and new is not False:
            raise SettingsDenied(f"`{path}` may only be turned off from chat; turn it on in the dashboard")
        if d == "to_true" and new is not True:
            raise SettingsDenied(f"`{path}` may only be turned on from chat")
    return old, new, spec


def _known_connectors() -> set[str]:
    try:
        from . import registry
        return set(registry.get().connectors)
    except Exception:
        return set()


@dataclass
class Change:
    path: str
    old: Any
    new: Any
    risk: str


def risk_of(changes: list[Change]) -> list[str]:
    return [r for r in RISK_ORDER if any(c.risk == r for c in changes)]


def check_chat(s: Settings, changes: Any) -> list[Change]:
    if not isinstance(changes, dict) or not changes:
        raise SettingsDenied("changes must be a non-empty object of {settings path: new value}")
    if len(changes) > MAX_PATHS:
        raise SettingsDenied(f"at most {MAX_PATHS} settings per proposal (got {len(changes)})")
    out: list[Change] = []
    seen: set[str] = set()
    for raw, val in changes.items():  # hard-deny first, for every path, before anything else is looked at
        parse(str(raw))
    for raw, val in changes.items():
        path = canon(raw)
        if path in seen:
            raise SettingsDenied(f"`{path}` given twice")
        seen.add(path)
        old, new, spec = _check_value(s, path, val, owner=False)
        if new == old:
            raise SettingsDenied(f"`{path}` is already {fmt(old)}")
        out.append(Change(path, old, new, spec.risk))
    if len(risk_of(out)) > 1:
        raise SettingsDenied("one risk class per proposal; these span " + " and ".join(risk_of(out)) + ": propose them separately")
    return out


def check_owner(s: Settings, values: Any, what: str, budget_strict: bool = False) -> list[Change]:
    """Validate an owner-authored set of values (a profile, or the diff of a backup); no-op paths are dropped."""
    if not isinstance(values, dict):
        raise SettingsDenied(f"{what} must be a mapping of settings path to value")
    for raw in values:
        parse(str(raw))
    out: list[Change] = []
    for raw, val in values.items():
        path = canon(raw)
        old, new, spec = _check_value(s, path, val, owner=True)
        if new == old:
            continue
        if budget_strict and spec.kind == "budget" and (old is not None and new > old):
            raise SettingsDenied(f"`{path}` would be raised; use the dashboard")
        out.append(Change(path, old, new, spec.risk))
    return out


# --- reading ---------------------------------------------------------------------------------------------------
def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def settings_file() -> Path:
    f = config.path("settings.yaml")
    if not f.exists():
        config.load()  # creates it
    return f


def file_hash() -> str:
    return sha(settings_file().read_bytes())


def flatten(s: Settings) -> dict[str, Any]:
    d = s.model_dump(mode="json")
    out: dict[str, Any] = {}
    for k, v in d.items():
        if k == "routing":
            for p in v:
                pid = p["id"]
                out[f"routing[{pid}]"] = "present"
                for fk, fv in p.items():
                    out[f"routing[{pid}].{fk}"] = fv
        elif k in ("router", "front_door", "research", "commands", "discord_door", "oauth") and isinstance(v, dict):
            for fk, fv in v.items():
                out[f"{k}.{fk}"] = fv
        else:
            out[k] = v
    return out


def _paths(s: Settings) -> list[str]:
    out = list(SPECS)
    for p in s.routing:
        out += [f"routing[{p.id}].{f}" for f in PROVIDER_SPECS]
    return out


def get(path: str | None = None) -> dict:
    """Allowlisted paths with their current values, a provider summary and the profiles. Never a redacted dump."""
    s = config.load()
    if path and hard_denied(path):
        return {"error": f"`{_safe(path, 60)}` can't be read or changed from chat; use settings.yaml or the dashboard"}
    out = {}
    for p in _paths(s):
        if path and not (p == path or p.startswith(path + ".") or p.startswith(path + "[")):
            continue
        key, _ = parse(p)
        spec = spec_for(key)
        out[p] = {"value": current(s, p), "change": _describe(spec), "risk": spec.risk}
    res: dict[str, Any] = {"settings": out}
    if not path:
        res["providers"] = [{"id": p.id, "kind": p.kind, "trust": p.trust, "enabled": p.enabled} for p in s.routing]
        res["profiles"] = profiles_summary(s)
        res["backups"] = [b["backup_id"] for b in list_backups()[:10]]
        res["active_profile"] = next((p["name"] for p in res["profiles"] if p.get("active")), None)
        res["note"] = ("Only these paths can be changed from chat, and only in the direction shown; everything else "
                       "(credentials, hosts, approvals, notify channels, programs to run, budgets upward) is edited "
                       "in settings.yaml or the dashboard.")
    return res


def _describe(spec: Spec) -> str:
    bits = {"any": "either direction", "down": "lower only", "up": "raise only", "to_false": "turn off only",
            "to_true": "turn on only", "add_only": "add only"}[spec.direction]
    if spec.choices:
        bits += "; one of " + ", ".join(spec.choices)
    if spec.lo is not None:
        bits += f"; min {spec.lo:g}"
    if spec.hi is not None:
        bits += f"; max {spec.hi:g}"
    return bits


# --- profiles --------------------------------------------------------------------------------------------------
def profile_changes(s: Settings, name: str) -> list[Change]:
    named = {str(k): v for k, v in s.profiles.items()}
    prof = named.get(name)
    if prof is None:
        raise SettingsDenied(f"no profile `{_safe(name, 40)}` (profiles: {', '.join(sorted(named)) or 'none defined'})")
    return check_owner(s, prof, f"profile `{name}`")


def profiles_summary(s: Settings) -> list[dict]:
    out = []
    for name, prof in sorted((str(k), v) for k, v in s.profiles.items()):
        row: dict[str, Any] = {"name": name, "paths": sorted(str(k) for k in prof) if isinstance(prof, dict) else []}
        try:
            ch = profile_changes(s, name)
            row["valid"] = True
            row["active"] = not ch and bool(prof)
            row["would_change"] = [c.path for c in ch]
        except SettingsDenied as e:
            row.update(valid=False, active=False, error=str(e))
        out.append(row)
    return out


# --- proposals (called by the executor before hashing) ---------------------------------------------------------
def _safe(text: Any, n: int = 200) -> str:
    return clean(str(text), n)


def check_proposer(caller) -> str | None:
    """Why this caller may not even propose a settings change, or None."""
    from . import shell
    if caller.untrusted or caller.approved_write:
        return "settings can't be changed from a run that read third-party content; start a fresh chat"
    if shell.proposer_class(caller) != "owner" or caller.name.startswith(("approval:", "job:", "watch:")):
        return "settings can only be changed from a chat the owner started, not from jobs, watches or approval follow-ups"
    return None


def check_limits(caller) -> str | None:
    now = time.time()
    ph = ",".join("?" * len(TOOLS))
    n = db.q(f"SELECT COUNT(*) AS n FROM approvals WHERE tool IN ({ph}) AND requested_by=? AND created>?",
             *sorted(TOOLS), caller.name, now - 3600)[0]["n"]
    if n >= RATE_LIMIT_PER_HOUR:
        return f"at most {RATE_LIMIT_PER_HOUR} settings proposals per hour; ask the owner to use the dashboard"
    where, who = ("thread_id=?", caller.thread_id) if caller.thread_id else ("requested_by=?", caller.name)
    d = db.q(f"SELECT COUNT(*) AS n FROM approvals WHERE tool IN ({ph}) AND status='denied' AND decided>? AND {where}",
             *sorted(TOOLS), now - COOLDOWN_SECONDS, who)[0]["n"]
    if d:
        return "the owner denied a settings change here less than 30 minutes ago; don't propose another now"
    return None


def prepare(caller, name: str, args: dict, reason: str = "") -> dict:
    """Gate and normalise a settings proposal. Returns the exact arguments to hash and show the owner (the model's
    own hidden fields are discarded and rebuilt here); raises SettingsDenied."""
    why = check_proposer(caller) or check_limits(caller)
    if why:
        raise SettingsDenied(why)
    s = config.load()
    base = file_hash()
    note = str((args.get("reason") if isinstance(args, dict) else "") or reason or "")[:300]
    out: dict[str, Any] = {"reason": note, "_base_hash": base, "_surface": str(caller.surface or "")[:20]}
    if name == TOOL_SET:
        ch = check_chat(s, args.get("changes"))
        out["changes"] = {c.path: c.new for c in ch}
        if not note:
            raise SettingsDenied("give a short reason for the change")
    elif name == TOOL_PROFILE:
        pn = str(args.get("name") or "")
        ch = profile_changes(s, pn)
        if not ch:
            raise SettingsDenied(f"profile `{_safe(pn, 40)}` already matches the current settings")
        out["name"] = pn
        out["_changes"] = {c.path: c.new for c in ch}
    elif name == TOOL_REVERT:
        bid = str(args.get("backup_id") or "")
        bsha, bsettings = load_backup(bid)
        ch = revert_changes(s, bsettings)
        if not ch:
            raise SettingsDenied("that backup matches the current settings on every path chat can change")
        out["backup_id"] = bid
        out["_backup_sha"] = bsha
        out["_changes"] = {c.path: c.new for c in ch}
    else:
        raise SettingsDenied(f"unknown settings tool {name}")
    out["_old"] = {c.path: c.old for c in ch}
    out["_risk"] = risk_of(ch)
    return out


# --- backups and revert ----------------------------------------------------------------------------------------
_BACKUP_ID = re.compile(r"^settings-\d{8}-\d{6}-[0-9a-f]{8}(-\d{1,3})?$")


def backup_dir() -> Path:
    d = config.path("settings-backups", ".keep").parent
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_backups() -> list[dict]:
    out = []
    for f in sorted(backup_dir().glob("settings-*.yaml"), reverse=True):
        out.append({"backup_id": f.stem, "created": f.stat().st_mtime, "sha256": sha(f.read_bytes())[:12]})
    return out


def load_backup(backup_id: str) -> tuple[str, Settings]:
    if not _BACKUP_ID.match(backup_id or ""):
        raise SettingsDenied("backup_id must be one from pmjc_settings_get (settings-YYYYMMDD-HHMMSS-xxxxxxxx)")
    f = backup_dir() / f"{backup_id}.yaml"
    if not f.is_file():
        raise SettingsDenied(f"no backup {backup_id}")
    raw = f.read_bytes()
    try:
        return sha(raw), Settings.model_validate(yaml.safe_load(raw.decode("utf-8")) or {})
    except Exception as e:
        raise SettingsDenied(f"backup {backup_id} isn't a valid settings file ({type(e).__name__})") from e


def revert_changes(cur: Settings, backup: Settings) -> list[Change]:
    """Diff between the current settings and a backup, restricted to allowlisted paths. A difference on any other
    path (hard-denied or simply not allowlisted) refuses the whole revert and points to the dashboard."""
    a, b = flatten(cur), flatten(backup)
    diff = sorted(k for k in set(a) | set(b) if a.get(k, "<absent>") != b.get(k, "<absent>"))
    bad = []
    values: dict[str, Any] = {}
    for k in diff:
        try:
            parse(k)
            values[k] = b[k] if k in b else None
        except SettingsDenied:
            bad.append(k)
    if bad:
        raise SettingsDenied("that backup differs from the current settings on paths chat can't change ("
                             + ", ".join(bad[:8]) + ("..." if len(bad) > 8 else "") + "); review and restore it in the dashboard")
    return check_owner(cur, values, "backup", budget_strict=True)


# --- applying (runs after the owner approved) ------------------------------------------------------------------
def _patch(text: str, items: list[Change], old: Settings) -> str:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    doc = y.load(text) if text.strip() else None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise SettingsError("settings.yaml isn't a mapping; edit it by hand")
    for c in items:
        key, pid = parse(c.path)
        if pid is not None:
            field = key.split(".", 1)[1]
            routing = doc.get("routing")
            if routing is None:  # providers come from built-in defaults: the list has to be written out once
                routing = []
                for p in old.routing:
                    m = CommentedMap()
                    for k, v in p.model_dump(mode="json", exclude_defaults=True).items():
                        m[k] = v
                    routing.append(m)
                doc["routing"] = routing
            entry = next((e for e in routing if isinstance(e, dict) and e.get("id") == pid), None)
            if entry is None:
                raise SettingsError(f"provider {pid} isn't in settings.yaml's routing list")
            entry[field] = c.new
            continue
        parts = key.split(".")
        node = doc
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = CommentedMap()
                node[part] = nxt
            node = nxt
        node[parts[-1]] = c.new
    buf = io.StringIO()
    y.dump(doc, buf)
    return buf.getvalue()


def _violations(s: Settings) -> set[str]:
    v: set[str] = set()
    ids = [p.id for p in s.routing]
    if len(ids) != len(set(ids)):
        v.add("duplicate provider ids")
    known = set(ids)
    builtin = {"default", "strong", "local"}
    for name, members in s.router.tiers.items():
        for m in members:
            if m not in known:
                v.add(f"tier {name} references unknown provider {m}")
        if name == "local":
            for m in members:
                p = next((x for x in s.routing if x.id == m), None)
                if p is not None and p.trust != "local":
                    v.add(f"tier local lists non-local provider {m}")
    for r in s.router.route_rules:
        if r.tier not in builtin and r.tier not in s.router.tiers:
            v.add(f"route rule points at unknown tier {r.tier}")
    for p in s.routing:
        if p.monthly_budget_usd is not None and p.monthly_budget_usd < 0:
            v.add(f"negative budget on {p.id}")
    return v


def check_invariants(old: Settings, new: Settings) -> None:
    bad = sorted(_violations(new) - _violations(old))
    if not any(p.enabled for p in new.routing):
        bad.append("no model provider would stay enabled")
    o = {p.id: p for p in old.routing}
    for p in new.routing:
        q = o.get(p.id)
        if q is None:
            bad.append(f"provider {p.id} appeared")
            continue
        if q.trust != p.trust:
            bad.append(f"trust of {p.id} changed")
        if q.monthly_budget_usd is not None and p.monthly_budget_usd is None:
            bad.append(f"budget of {p.id} would become unlimited")
    if len(o) != len(new.routing):
        bad.append("providers were removed")
    if bad:
        raise SettingsError("refused: " + "; ".join(bad))


def apply(kind: str, changes: dict, base_hash: str | None, backup_sha: str | None = None, backup_id: str | None = None) -> dict:
    """Apply an approved change set. kind: set | profile | revert. Raises SettingsError when the file changed since
    the proposal or the result doesn't validate. Returns the diff and hashes for the audit."""
    if not base_hash or not isinstance(changes, dict) or not changes:
        raise SettingsError("this tool only runs from an approved proposal")
    with config.file_lock():
        f = settings_file()
        raw = f.read_bytes()
        cur = sha(raw)
        if cur != base_hash:
            raise SettingsError("settings changed since this was proposed (the file was edited); settings changed, propose again")
        text = raw.decode("utf-8")
        old = Settings.model_validate(yaml.safe_load(text) or {})
        try:
            if kind == "set":
                items = check_chat(old, changes)
            elif kind == "profile":
                items = check_owner(old, changes, "profile")
            elif kind == "revert":
                bsha, bset = load_backup(backup_id or "")
                if bsha != backup_sha:
                    raise SettingsError("the backup file changed since this was proposed")
                items = revert_changes(old, bset)
                if {c.path: c.new for c in items} != changes:
                    raise SettingsError("settings changed, propose again")
            else:
                raise SettingsError("unknown apply kind")
        except SettingsDenied as e:
            raise SettingsError(f"refused: {e}") from e
        if kind != "set" and {c.path: c.new for c in items} != changes:
            raise SettingsError("settings changed, propose again")
        new_text = _patch(text, items, old)
        try:
            new = Settings.model_validate(yaml.safe_load(new_text) or {})
        except Exception as e:
            raise SettingsError(f"the result isn't valid settings ({type(e).__name__}); nothing written") from e
        check_invariants(old, new)
        a, b = flatten(old), flatten(new)
        changed = {k for k in set(a) | set(b) if a.get(k, "<absent>") != b.get(k, "<absent>")}
        if changed != {c.path for c in items}:
            raise SettingsError("the patch would change more than was approved; nothing written")
        bid = _backup(raw, cur)
        tmp = f.with_name(f.name + ".new")
        tmp.write_bytes(new_text.encode("utf-8"))
        os.replace(tmp, f)
        new_hash = sha(new_text.encode("utf-8"))
    return {"applied": True, "diff": {c.path: {"old": c.old, "new": c.new} for c in items}, "base_hash": cur,
            "new_hash": new_hash, "backup_id": bid}


def _backup(raw: bytes, h: str) -> str:
    d = backup_dir()
    stem = f"settings-{time.strftime('%Y%m%d-%H%M%S')}-{h[:8]}"
    bid, n = stem, 0
    while (d / f"{bid}.yaml").exists():
        n += 1
        bid = f"{stem}-{n}"
    (d / f"{bid}.yaml").write_bytes(raw)
    for old in sorted(d.glob("settings-*.yaml"))[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return bid


def audit_applied(a: dict, res: Any, decided_by: str) -> None:
    """Audit rows for an applied change: hashes, approver, surface, thread, then one row per path."""
    args = json.loads(a["args"])
    if not isinstance(res, dict) or not res.get("applied"):
        return
    h = a["payload_hash"]
    db.audit(decided_by, a["tool"], h, "settings_applied",
             f"base={str(res.get('base_hash'))[:12]} new={str(res.get('new_hash'))[:12]} by={decided_by} "
             f"surface={args.get('_surface', '')} thread={a.get('thread_id') or ''} backup={res.get('backup_id')}")
    for p, d in (res.get("diff") or {}).items():
        db.audit(decided_by, a["tool"], h, "settings_diff", f"{p}: {fmt(d['old'])} -> {fmt(d['new'])}")


# --- rendering (Discord, link page, dashboard) -----------------------------------------------------------------
_INVISIBLE = {"Cf", "Cc", "Cs", "Co", "Cn", "Zl", "Zp"}


def clean(text: Any, limit: int = 200) -> str:
    """Strip control, zero-width, bidi and other invisible characters; newlines become spaces; clip."""
    s = str(text).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) not in _INVISIBLE)
    s = re.sub(r" {2,}", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def fmt(v: Any) -> str:
    if v is None:
        return "unset"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(clean(i, 40) for i in v) + "]"
    return clean(v, 80)


def _diff_line(old: Any, new: Any) -> str:
    if isinstance(old, list) and isinstance(new, list):
        add = [clean(i, 40) for i in new if i not in old]
        rem = [clean(i, 40) for i in old if i not in new]
        return " ".join([f"+{i}" for i in add] + [f"−{i}" for i in rem]) or "(no change)"
    return f"{fmt(old)} → {fmt(new)}"


def view(a: dict) -> dict | None:
    """Structured, already cleaned (not yet escaped for a medium) description of a settings approval; None for other tools."""
    tool = a.get("tool")
    if tool not in TOOLS:
        return None
    try:
        args = json.loads(a.get("args") or "{}")
    except ValueError:
        return None
    old = args.get("_old") or {}
    lines = []
    for path, new in (args.get("changes") or args.get("_changes") or {}).items():
        o, n = old.get(path), new
        lines.append({"path": clean(path, 80), "old": o, "new": n, "text": _diff_line(o, n),
                      "is_list": isinstance(o, list) and isinstance(n, list)})
    risk = [r for r in args.get("_risk") or [] if r in RISK_ORDER]
    badge = (risk[-1] if risk else OFF_LOWER).upper()
    title = {TOOL_SET: "Settings change", TOOL_REVERT: f"Settings revert to {clean(args.get('backup_id', ''), 40)}",
             TOOL_PROFILE: f"Switch profile {clean(args.get('name', ''), 40)}"}[tool]
    return {"title": title, "badge": badge, "risk": risk, "lines": lines,
            "two_step": bool(set(risk) & TWO_STEP_CLASSES), "surface": clean(args.get("_surface", ""), 20),
            "reason": clean(args.get("reason") or a.get("reason") or "", 300)}


def needs_review(a: dict) -> bool:
    v = view(a)
    return bool(v and v["two_step"])


def _md(text: str) -> str:
    """Neutralise Markdown and mentions in free text (the model's reason)."""
    t = re.sub(r"([\\`*_~|>#\[\]()<:!-])", r"\\\1", text)
    return t.replace("@", "＠")


def _code(text: str) -> str:
    return "`" + str(text).replace("`", "'").replace("@", "＠") + "`"


REVIEWED_MARK = "Routing or cost change: reviewed"


def discord_text(a: dict, reviewed: bool = False) -> str:
    """The Discord approval message: risk badge, one line per path (old → new), the model's reason last."""
    v = view(a)
    head = f"**[{v['badge']}] {_md(v['title'])}**" + (f" · via {_md(v['surface'])}" if v["surface"] else "")
    rows = []
    for ln in v["lines"]:
        if ln["is_list"]:
            rows.append(f"{_code(ln['path'])}: {_code(ln['text'])}")
        else:
            o, n = (fmt(ln["old"]), fmt(ln["new"]))
            rows.append(f"{_code(ln['path'])}: {_code(o)} → {_code(n)}")
    out = [head, *rows]
    if v["reason"]:
        out.append(f"> model's claim: {_md(v['reason'])}")
    if v["two_step"]:
        out.append("-# " + (REVIEWED_MARK + ", now approve or deny." if reviewed else "Routing or cost change: press Review, then Approve."))
    return "\n".join(out)[:1900]


def plain_text(a: dict) -> str:
    v = view(a)
    rows = [f"{ln['path']}: {ln['text']}" for ln in v["lines"]]
    tail = [f"model's claim: {v['reason']}"] if v["reason"] else []
    return "\n".join([f"[{v['badge']}] {v['title']}" + (f" (via {v['surface']})" if v["surface"] else ""), *rows, *tail])


def html_view(a: dict) -> str:
    """HTML (escaped) for the approval link page."""
    import html as h
    v = view(a)
    rows = "".join(f"<tr><td><code>{h.escape(ln['path'])}</code></td><td>{h.escape(ln['text'])}</td></tr>" for ln in v["lines"])
    reason = f"<p><i>model's claim:</i> “{h.escape(v['reason'])}”</p>" if v["reason"] else ""
    return (f"<p><b>[{h.escape(v['badge'])}]</b> {h.escape(v['title'])}"
            + (f" · via {h.escape(v['surface'])}" if v["surface"] else "") + f"</p><table>{rows}</table>{reason}")


def api_view(a: dict) -> dict | None:
    """JSON for the dashboard (plain strings; the page escapes them)."""
    v = view(a)
    if v is None:
        return None
    return {"title": v["title"], "badge": v["badge"], "two_step": v["two_step"], "surface": v["surface"],
            "reason": v["reason"], "lines": [{"path": ln["path"], "text": ln["text"]} for ln in v["lines"]]}


# --- owner-typed chat commands (/settings, /set, /profile, /revert): no model involved -----------------------------
def parse_set_args(arg: str) -> dict:
    """`/set a.b=1 c=[x, y]` or `/set a.b 1`: values are YAML. Returns {path: value}; raises SettingsDenied."""
    arg = arg.strip()
    if not arg:
        raise SettingsDenied("usage: /set <path> <value>   or   /set a=1 b=2   (see /settings for the paths)")
    pairs = []
    if re.match(r"^[A-Za-z_][\w.\[\]-]*=", arg):
        for p in re.split(r"\s+(?=[A-Za-z_][\w.\[\]-]*=)", arg):
            k, _, v = p.partition("=")
            pairs.append((k.strip(), v.strip()))
    else:
        k, _, v = arg.partition(" ")
        pairs.append((k.strip(), v.strip()))
    out: dict[str, Any] = {}
    for k, v in pairs:
        if not v:
            raise SettingsDenied(f"no value for `{_safe(k, 60)}`")
        try:
            out[k] = yaml.safe_load(v)
        except yaml.YAMLError as e:
            raise SettingsDenied(f"can't read the value for `{_safe(k, 60)}` as YAML") from e
    return out


def settings_text(path: str | None = None) -> str:
    r = get(path or None)
    if "error" in r:
        return r["error"]
    lines = ["**Settings chat may change** (`/set <path> <value>`)"]
    for p, d in r["settings"].items():
        lines.append(f"- {_code(p)} = {_code(fmt(d['value']))} ({d['risk']}; {d['change']})")
    if "providers" in r:
        lines.append("\n**Providers:** " + ", ".join(
            f"{_code(p['id'])} ({p['trust']}{'' if p['enabled'] else ', off'})" for p in r["providers"]))
        lines.append("**Profiles:** " + profiles_line(r["profiles"]) + " (`/profile <name>`)")
        if r.get("backups"):
            lines.append("**Backups:** `/revert` lists them.")
    return "\n".join(lines)


def profiles_line(prof: list[dict]) -> str:
    return ", ".join(f"{_code(clean(p['name'], 30))}{' (active)' if p.get('active') else ''}{'' if p.get('valid') else ' (invalid)'}"
                     for p in prof) or "none; the owner defines them in settings.yaml"


def _owner_ok(caller) -> str | None:
    if not caller.can("actions"):
        return "changing settings needs a token with the `actions` scope (or admin)"
    if caller.untrusted:
        return "this conversation read third-party content; start a new chat"
    return None


async def _approval_reply(caller, tool: str, args: dict) -> str:
    from . import executor
    from .chat_commands import approval_line
    res = await executor.request_approval(caller, tool, args, "typed by the owner as a chat command")
    row = db.q("SELECT id, tool, args, reason FROM approvals WHERE id=?", res["approval_id"])[0]
    return (discord_text(row) + "\n" + approval_line({"id": row["id"], "tool": tool, "reason": ""})
            + "\nNothing changes until you approve.")


def _owner_args(caller, extra: dict) -> dict:
    out = dict(extra)
    out.update({"reason": "typed by the owner as a chat command", "_base_hash": file_hash(),
                "_surface": str(caller.surface or "v1")[:20]})
    return out


async def owner_command(name: str, arg: str, caller, thread_id: str | None = None) -> str:
    """Run one owner command. off-lower /set applies at once (lock, compare-and-swap, backup, audit marked as an owner
    command); routing/cost /set, /profile and /revert reply with an approval (diff plus signed links)."""
    why = _owner_ok(caller)
    if why:
        return why
    try:
        if name == "settings":
            return settings_text(arg or None)
        s = config.load()
        if name == "set":
            ch = check_chat(s, parse_set_args(arg))
            changes = {c.path: c.new for c in ch}
            if set(risk_of(ch)) <= {OFF_LOWER}:
                res = apply("set", changes, file_hash())
                _audit_direct(caller, thread_id, res)
                return ("**Applied** (backup `" + res["backup_id"] + "`)\n"
                        + "\n".join(f"{_code(p)}: {_code(fmt(d['old']))} → {_code(fmt(d['new']))}" for p, d in res["diff"].items())
                        + "\n`/revert " + res["backup_id"] + "` undoes it (with approval).")
            args = _owner_args(caller, {"changes": changes, "_old": {c.path: c.old for c in ch}, "_risk": risk_of(ch)})
            return await _approval_reply(caller, TOOL_SET, args)
        if name == "profile":
            if not arg:
                return "**Profiles:** " + profiles_line(profiles_summary(s))
            ch = profile_changes(s, arg.strip())
            if not ch:
                return f"Profile {_code(clean(arg, 40))} already matches the current settings."
            args = _owner_args(caller, {"name": arg.strip(), "_changes": {c.path: c.new for c in ch},
                                        "_old": {c.path: c.old for c in ch}, "_risk": risk_of(ch)})
            return await _approval_reply(caller, TOOL_PROFILE, args)
        if name == "revert":
            if not arg:
                bs = list_backups()[:10]
                if not bs:
                    return "No backups yet."
                return "**Backups** (newest first; `/revert <id>`):\n" + "\n".join(f"- `{b['backup_id']}`" for b in bs)
            bid = arg.strip().split()[0]
            bsha, bset = load_backup(bid)
            ch = revert_changes(s, bset)
            if not ch:
                return "That backup matches the current settings on every path chat can change."
            args = _owner_args(caller, {"backup_id": bid, "_backup_sha": bsha, "_changes": {c.path: c.new for c in ch},
                                        "_old": {c.path: c.old for c in ch}, "_risk": risk_of(ch)})
            return await _approval_reply(caller, TOOL_REVERT, args)
    except SettingsDenied as e:
        return f"Refused: {e}"
    except SettingsError as e:
        return f"Not applied: {e}"
    return "unknown settings command"


def _audit_direct(caller, thread_id: str | None, res: dict) -> None:
    h = hashlib.sha256(json.dumps(res["diff"], sort_keys=True, default=str).encode()).hexdigest()
    db.audit(caller.name, TOOL_SET, h, "settings_applied",
             f"owner command base={res['base_hash'][:12]} new={res['new_hash'][:12]} by={caller.name} "
             f"surface={caller.surface or 'v1'} thread={thread_id or ''} backup={res['backup_id']}")
    for p, d in res["diff"].items():
        db.audit(caller.name, TOOL_SET, h, "settings_diff", f"{p}: {fmt(d['old'])} -> {fmt(d['new'])}")
