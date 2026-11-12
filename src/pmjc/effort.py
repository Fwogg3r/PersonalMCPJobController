"""Thinking effort per run: how hard a reasoning model thinks before it answers or calls a tool.

Off by default (`thinking.control: none` sends nothing, so the model uses its own default). With a control set, each
run gets a level, `off | low | medium | high`:
- policy `fixed`: always `thinking.default`.
- policy `adaptive`: chosen by rules at the start of the run, never by a model call (see DECISIONS, "Thinking effort"):
  an effort prefix (`/fast`, `/think`) wins; a `strong` route tier means high; otherwise the highest `effort_hint` of
  the connectors the run selected (owner overrides in `router.connector_effort`), at least medium when several
  connectors are selected or the request reads like several steps or like building something, and medium when no
  connector was selected (an open request).
- escalation: a run that shows a failure signal (a tool error, a denied call, a loader loop, the same call twice) goes
  up one level for the rest of the run, once. Changing the level re-reads the prompt once (the level is written into
  the system message by the chat template), so it never changes back down mid-run.

How the level reaches the model (`thinking.control`): `param` sends a top-level `reasoning_effort` (LM Studio
accepts none, minimal, low, medium, high, xhigh and passes it to the model's template; measured 2026-11-11, LM Studio
ignores `chat_template_kwargs`), `kwargs` sends `chat_template_kwargs` (`enable_thinking`, `reasoning_effort`) for
servers that forward them, `none` sends nothing. `thinking.levels` maps pmjc's levels (off included) to the server's
values.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import config, registry

LEVELS = ("off", "low", "medium", "high")
DEFAULT_HINT = "medium"  # connectors without an effort_hint
_MULTI_STEP = re.compile(r"\b(and then|then|after that|afterwards|next|first|finally|compare|step|steps|plan|each|all of)\b", re.I)
_BUILD = re.compile(r"\b(code|coding|script|refactor|debug|fix|implement|build|write a|design|analy[sz]e|investigate|"
                    r"why does|why is|figure out|migrate|deploy|research)\b", re.I)


def rank(level: str) -> int:
    return LEVELS.index(level) if level in LEVELS else LEVELS.index(DEFAULT_HINT)


def up(level: str) -> str:
    return LEVELS[min(len(LEVELS) - 1, rank(level) + 1)]


def strip_prefix(text: str) -> tuple[str, str | None]:
    """An effort prefix at the start of a request (`/fast what's playing`): the rest of the text and its level."""
    t = (text or "").lstrip()
    for prefix, level in sorted(config.load().router.effort_prefixes.items(), key=lambda kv: -len(kv[0])):
        if prefix and t.lower().startswith(prefix.lower()) and (len(t) == len(prefix) or t[len(prefix)].isspace()):
            return t[len(prefix):].strip(), level if level in LEVELS else None
    return text, None


def hint_of(cid: str) -> str:
    over = config.load().router.connector_effort.get(cid)
    if over in LEVELS:
        return over
    c = registry.get().connectors.get(cid)
    h = getattr(c, "effort_hint", None) if c is not None else None
    return h if h in LEVELS else DEFAULT_HINT


@dataclass
class Effort:
    """The run's level and how it got there (logged with every model call and in the audit log)."""
    level: str
    reason: str
    escalated: bool = False
    trace: list[str] = field(default_factory=list)
    _seen: set = field(default_factory=set, repr=False)
    _loader_calls: int = 0

    def failure(self, name: str, args: dict, out: str) -> str | None:
        """Look at one tool call's outcome; returns why the run should think harder, if it should."""
        key = name + json.dumps(args, sort_keys=True, default=str)
        repeat = key in self._seen
        self._seen.add(key)
        if name == "pmjc_capabilities":
            self._loader_calls += 1
        if out.startswith("ERROR") or out.startswith('{"error"'):
            return f"{name} failed"
        if "Denied:" in out[:200]:
            return f"{name} was denied"
        if repeat:
            return f"{name} called twice with the same arguments"
        if self._loader_calls >= 2:
            return "looping on pmjc_capabilities"
        return None

    def escalate(self, why: str, enabled: bool) -> bool:
        """Raise the level once per run. True when the level changed (the next call re-reads the prompt)."""
        if not enabled or self.escalated or self.level == "high":
            return False
        old, self.level, self.escalated = self.level, up(self.level), True
        self.trace.append(f"{old}->{self.level}: {why}")
        return True


def choose(p: config.ProviderConfig, prompt: str, caller, tier: str = "") -> Effort | None:
    """The run's starting level, or None when the provider has no thinking control."""
    t = p.thinking
    if t.control == "none":
        return None
    forced = getattr(caller, "effort_override", None)
    if forced in LEVELS:
        return Effort(forced, "prefix")
    if t.policy != "adaptive":
        return Effort(t.default if t.default in LEVELS else "high", "fixed")
    if tier == "strong":
        return Effort("high", "tier strong")
    sel = getattr(caller, "tool_select", None)
    picked = sorted(sel.loaded) if sel is not None else []
    text = getattr(caller, "select_hint", None) or prompt or ""
    if not picked:
        level, why = DEFAULT_HINT, "no connector selected"
    else:
        hints = {cid: hint_of(cid) for cid in picked}
        level = max(hints.values(), key=rank)
        why = "hints " + ", ".join(f"{c}={h}" for c, h in hints.items())
        if len(picked) > 1 and rank(level) < rank("medium"):
            level, why = "medium", why + "; several connectors"
    if _BUILD.search(text) and rank(level) < rank("high"):
        level, why = "high", why + "; reads like building or investigating"
    elif (_MULTI_STEP.search(text) or len(text) > 400) and rank(level) < rank("medium"):
        level, why = "medium", why + "; several steps"
    floor = t.adaptive_floor if t.adaptive_floor in LEVELS else "off"
    if rank(level) < rank(floor):
        level, why = floor, why + f"; floor {floor}"
    return Effort(level, why)


def apply(body: dict, p: config.ProviderConfig, e: Effort | None) -> None:
    """Write the level into a chat-completions request body."""
    if e is None or p.thinking.control == "none":
        return
    on = e.level != "off"
    value = p.thinking.levels.get(e.level, e.level)
    if p.thinking.control == "param":  # LM Studio, OpenAI-style servers: none | minimal | low | medium | high | xhigh
        body["reasoning_effort"] = value if on else p.thinking.levels.get("off", "none")
    elif p.thinking.control == "kwargs":  # servers that pass chat_template_kwargs to the template (vLLM, llama.cpp)
        kw = {"enable_thinking": on}
        if on:
            kw["reasoning_effort"] = value
        body["chat_template_kwargs"] = kw
