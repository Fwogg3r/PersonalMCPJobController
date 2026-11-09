"""Tool briefs and loading on demand for small-context models (provider option `tool_mode: select`, phase4/w step 2).

Instead of every tool schema, a select-mode run gets: the core tools in full, the connectors picked before turn 1
in full, and one line per remaining connector ("- ebay: Search eBay listings ... (ebay_search)"). Picked means an
alias/tag match on the request and on *trusted* thread turns, plus the thread's sticky set (connectors loaded or used
by earlier runs of the thread). `pmjc_capabilities(task)` doubles as the loader: it marks matching connectors, whose
schemas join the tool list from the next turn (one batch per run, so the prompt prefix stays cache-friendly).

Loading a schema grants nothing. Every call still goes through the executor (scopes, `allowed_tools`, taint,
approvals), so a call to a tool that was never sent just runs those same checks; wrong arguments for such a tool get
its compact schema back in the error. Order is deterministic: core tools, then connectors sorted by id, tools sorted
by name. The selected schemas never exceed a token budget (`select_budget_tokens`, default 30% of the context).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import capabilities, memory, registry
from .auth import Caller
from .config import ProviderConfig
from .registry import ToolSpec

# Always sent in full: orientation, context, threads, memory, contacts, watches and jobs, paging of clipped results.
CORE_TOOLS = frozenset({
    "pmjc_capabilities", "pmjc_connectors", "pmjc_context", "pmjc_result_get",
    "pmjc_threads_recent", "pmjc_thread_get", "pmjc_thread_note", "pmjc_thread_start", "pmjc_thread_close",
    "pmjc_memory_get", "pmjc_memory_search", "pmjc_memory_set", "pmjc_memory_delete",
    "pmjc_contact_find", "pmjc_contact_set",
    "pmjc_watch_start", "pmjc_watch_status", "pmjc_watch_cancel", "pmjc_watch_list", "pmjc_watch_wait",
    "pmjc_job_create", "pmjc_jobs_list"})

BRIEF_CHARS = 100  # a connector's line body (description plus tool names) is capped at this
DEFAULT_SHARE = 0.3  # of context_tokens
_STOP = frozenset("that this with from your their them they have will what when where which about into only some "
                  "such than then also more most other used uses using tool tools data user users send sends "
                  "read reads write writes the and for are not you can".split())
_DF_MAX = 3  # a derived tag shared by more connectors than this is too generic to select on


# --- matching ----------------------------------------------------------------------------------------------
def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _words(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]


def _padded(text: str) -> str:
    return " " + " ".join(_words(text)) + " "


_cache: dict = {"reg": None, "words": {}}


def derived_tags(c, reg: registry.Registry | None = None) -> set[str]:
    """Single-word tags from the connector's id, name and the capability catalog's task text. Words that
    many connectors share ("search", "message") are dropped, so they can't select everything."""
    reg = reg or registry.get()
    if _cache["reg"] is not reg:
        _cache["reg"], _cache["words"] = reg, {}
    words = _cache["words"]
    for o in reg.connectors.values():
        if o.type_id not in words:
            words[o.type_id] = _derive(o)
    mine = words.setdefault(c.type_id, _derive(c))
    others = [w for t, w in words.items() if t != c.type_id]
    return {w for w in mine if sum(w in o for o in others) < _DF_MAX}


def _derive(c) -> set[str]:
    words: set[str] = set(_words(c.type_id.replace("_", " ")))
    words |= set(_words(c.name))
    for e in capabilities.CATALOG:
        if c.type_id in e["needs"]:
            words |= set(_words(e["task"]))
    return {w for w in words if len(w) >= 4 and w not in _STOP}


def tags_of(c, reg: registry.Registry | None = None) -> tuple[set[str], set[str]]:
    """(phrases, words): explicit aliases (phrases may have several words) and derived single words."""
    explicit = {" ".join(_words(a)) for a in (getattr(c, "aliases", None) or []) if _words(a)}
    explicit |= {" ".join(_words(c.type_id.replace("_", " ")))}
    if c.instance:
        explicit.add(" ".join(_words(c.instance)))
    return {e for e in explicit if e}, derived_tags(c, reg)


def matches(text: str, c, reg: registry.Registry | None = None) -> bool:
    padded = _padded(text)
    phrases, words = tags_of(c, reg)
    return any(f" {p} " in padded for p in phrases) or any(f" {w} " in padded for w in words)


def brief_of(c, tools: list[ToolSpec]) -> str:
    """One line body for the system prompt: a short description, then the tool names, `BRIEF_CHARS` at most."""
    desc = " ".join((c.description or c.name or c.id).split())
    desc = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0].rstrip(".")
    names = ", ".join(sorted(t.name for t in tools))
    desc = desc[:55].rstrip() + ("..." if len(desc) > 55 else "")
    room = BRIEF_CHARS - len(desc) - 3
    if room < 8:
        return desc[:BRIEF_CHARS]
    if len(names) > room:
        names = names[:room - 3].rsplit(",", 1)[0] + ", ..."
    return f"{desc} ({names})"


def trusted_hint(thread_id: str | None, text: str, turns: int) -> str:
    """The request plus the thread's trusted recent turns: the only text that may select tools."""
    from . import ask
    if turns and thread_id:
        past, _tainted, _sens = ask.history(thread_id, turns, include_untrusted=False)
        if past:
            return past + "\n" + text
    return text


# --- the per-run selection -----------------------------------------------------------------------------------
@dataclass
class Selection:
    p: ProviderConfig
    caller: Caller
    core: list[ToolSpec]
    groups: dict[str, list[ToolSpec]]  # connector id -> its non-core tools the run may use
    loaded: set[str] = field(default_factory=set)
    budget: int = 0
    used: int = 0  # ~tokens of loaded connectors' schemas
    batch_done: bool = False  # the one on-demand batch of this run has been spent
    reload: bool = False

    # -- what is sent
    def tools(self) -> list[ToolSpec]:
        out = sorted(self.core, key=lambda t: t.name)
        for cid in sorted(self.loaded):
            out += sorted(self.groups[cid], key=lambda t: t.name)
        return out

    def sent_names(self) -> set[str]:
        return {t.name for t in self.tools()}

    def briefs(self) -> str:
        reg = registry.get()
        lines = []
        for cid in sorted(self.groups):
            if cid in self.loaded or (self.caller.untrusted and self.egress_only(cid)):
                continue
            c = reg.connectors.get(cid)
            if c is not None:
                lines.append(f"- {cid}: {brief_of(c, self.groups[cid])}")
        if not lines:
            return ""
        return ("\n\nMore tools exist but aren't loaded, to keep the prompt small. To use one, call "
                "pmjc_capabilities(task) with a few words (its tools load for your next turn) or just call the tool "
                "by name (arguments are checked; an error shows the schema):\n" + "\n".join(lines))

    # -- sizes and rules
    def cost(self, cids) -> int:
        from . import agent
        tools = [t for cid in cids for t in sorted(self.groups[cid], key=lambda t: t.name)]
        return agent._tokens(agent.tool_payload(self.p, tools, "", "openai", ordered=True)[0]) if tools else 0

    def egress_only(self, cid: str) -> bool:
        return bool(self.groups[cid]) and all(t.egress for t in self.groups[cid])

    def connector_of(self, name: str) -> str | None:
        return next((cid for cid, ts in self.groups.items() if any(t.name == name for t in ts)), None)

    def take_reload(self) -> bool:
        r, self.reload = self.reload, False
        return r

    def _remember(self, cids) -> None:
        memory.thread_tools_add(getattr(self.caller, "thread_id", None), cids)

    # -- the loader (called from pmjc_capabilities)
    def load(self, task: str | None, entries: list[dict]) -> dict | None:
        """Mark connectors matching `task` to load from the next turn. Returns a note for the model, or None."""
        if not task or not task.strip():
            return None
        reg = registry.get()
        want = {cid for cid in self.groups if matches(task, reg.connectors[cid], reg)}
        for e in entries:
            for need in e.get("needs") or []:
                want |= {cid for cid, c in reg.connectors.items() if cid in self.groups and c.type_id == need}
        want -= self.loaded
        if not want:
            return None
        if self.batch_done:
            return {"tools_loaded": [], "note": "Tools already loaded once in this run; no more can be added. "
                    "Call the tool you need by name (the error shows its schema), or tell the user."}
        notes, refused = [], []
        if self.caller.untrusted:
            blocked = sorted(c for c in want if self.egress_only(c))
            if blocked:
                notes.append(f"Not loaded: {', '.join(blocked)}. This run has read third-party content, so tools that "
                             "send data off the machine are refused; start a fresh request to use them.")
            want -= set(blocked)
        want_s = sorted(want)
        if want_s and self.used + self.cost(want_s) > self.budget:
            refused = want_s
        if refused:
            notes.append("too many tools; pick fewer (ask for one thing at a time, e.g. a single connector): "
                         f"loading {', '.join(refused)} would pass the tool budget.")
            want_s = []
        if not want_s:
            return {"tools_loaded": [], "note": " ".join(notes)}
        self.used += self.cost(want_s)
        self.loaded |= set(want_s)
        self.batch_done = True
        self.reload = True
        self._remember(want_s)
        out: dict[str, Any] = {"tools_loaded": want_s, "tool_names": sorted(t.name for c in want_s for t in self.groups[c]),
                               "note": " ".join(notes + ["These tools can be called from your next turn."])}
        if self.p.schema_mode == "compact":
            lims = {c: " ".join(reg.connectors[c].limitations.split()) for c in want_s if reg.connectors[c].limitations}
            if lims:
                out["limits"] = lims
        return out

    # -- calls (from the agent loop)
    def precheck(self, name: str, args: dict) -> str | None:
        """Before executing a call to a tool that wasn't sent: remember its connector; return an error carrying the
        compact schema when the arguments can't be right. None: go ahead (the executor decides everything else)."""
        cid = self.connector_of(name)
        if cid is None:
            return None  # core, unknown or not allowed: the executor handles it
        self._remember([cid])
        if name in self.sent_names():
            return None
        spec = next(t for t in self.groups[cid] if t.name == name)
        schema = registry.tool_json_schema(spec)
        props = set((schema.get("properties") or {}))
        why = None
        if not isinstance(args, dict):
            why = "arguments must be an object"
        else:
            missing = [k for k in schema.get("required") or [] if k not in args]
            extra = [k for k in args if k not in props and not k.startswith("_")]
            if missing:
                why = f"missing required: {', '.join(missing)}"
            elif extra:
                why = f"unknown arguments: {', '.join(extra)}"
        if why is None:
            return None
        return (f"ERROR: invalid arguments for {name} ({why}). It exists but its schema wasn't loaded; retry with "
                f"this: {registry.compact_tool_doc(spec)} {json.dumps(registry.compact_tool_schema(spec), separators=(',', ':'))}")

    def schema_hint(self, name: str) -> str:
        """For an error from a tool that wasn't sent: its compact schema, so the model can retry."""
        cid = self.connector_of(name)
        if cid is None or name in self.sent_names():
            return ""
        spec = next(t for t in self.groups[cid] if t.name == name)
        return (f" Schema of {name}: " + json.dumps(registry.compact_tool_schema(spec), separators=(",", ":")))


def begin(p: ProviderConfig, caller: Caller, tools: list[ToolSpec], prompt: str) -> Selection | None:
    """Pick the tools of a select-mode run. None for tool_mode all (or nothing to select among)."""
    if p.tool_mode != "select" or not tools:
        return None
    reg = registry.get()
    allowed = caller.allowed_tools
    usable = [t for t in tools if allowed is None or t.name in allowed]
    core = [t for t in usable if t.name in CORE_TOOLS]
    groups: dict[str, list[ToolSpec]] = {}
    for t in usable:
        if t.name not in CORE_TOOLS:
            groups.setdefault(t.connector, []).append(t)
    sel = Selection(p, caller, core, groups,
                    budget=p.select_budget_tokens if p.select_budget_tokens is not None
                    else int(p.context_tokens * DEFAULT_SHARE))
    text = getattr(caller, "select_hint", None) or prompt
    sticky = [c for c in memory.thread_tools(getattr(caller, "thread_id", None)) if c in groups]
    picked = [c for c in sorted(groups) if c not in sticky and c in reg.connectors and matches(text, reg.connectors[c], reg)]
    for cid in sticky + picked:
        if caller.untrusted and sel.egress_only(cid):
            continue
        cost = sel.cost([cid])
        if sel.used + cost > sel.budget:
            continue  # stays a brief
        sel.loaded.add(cid)
        sel.used += cost
    caller.tool_select = sel
    return sel


def end(caller: Caller) -> None:
    caller.tool_select = None
