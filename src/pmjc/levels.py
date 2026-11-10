"""Permission levels per tool (see DECISIONS.md "Run grants and permission levels").

- off: the tool is hidden from every tool list and refused (read and act tools).
- always_ask: every call is its own approval; never auto-approved (auto_approve lists don't apply). A person at the
  CLI or dashboard still runs it directly, except for the hard-floor tools below.
- ask: the default for act tools; one approval per call (later: coverable by a run grant).
- auto_clean: runs without approval while the run is clean (hasn't read untrusted content).

Precedence, strongest first:
1. hard floor in code: pmjc_shell_run and the settings tools are always_ask and need approval even at the CLI;
2. the owner's `tool_levels` in settings.yaml (exact tool name, else the longest matching glob);
3. the owner's `auto_approve` lists (global and an owner-written job's own): auto_clean;
4. the default the tool declares in code (`@tool(level=...)`), else `ask`.
Levels other than `off` only change anything for act tools.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

LEVELS = ("off", "always_ask", "ask", "auto_clean")


def hard_floor(name: str) -> bool:
    from . import settings_edit, shell
    return name == shell.TOOL or name in settings_edit.TOOLS


def configured(name: str, tool_levels: dict[str, str]) -> str | None:
    """The owner's level for `name` from `tool_levels`: an exact key wins, else the longest matching glob."""
    if name in tool_levels:
        return tool_levels[name]
    globs = [k for k in tool_levels if any(ch in k for ch in "*?[") and fnmatchcase(name, k)]
    return tool_levels[max(globs, key=len)] if globs else None


def effective(name: str, default: str | None, tool_levels: dict[str, str], auto_approve: set[str] | list[str] = ()) -> str:
    """The level that applies to tool `name` (see the precedence above). `auto_approve` is the union of the lists
    that apply to this caller (global settings plus an owner-written job's own)."""
    if hard_floor(name):
        return "off" if configured(name, tool_levels) == "off" else "always_ask"
    own = configured(name, tool_levels)
    if own:
        return own
    if name in auto_approve:
        return "auto_clean"
    return default or "ask"
