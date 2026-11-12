"""Permission levels per tool, and the tool groups that give them defaults (see DECISIONS.md "Run grants and
permission levels" and "Tool groups and the auto level").

- off: the tool is hidden from every tool list and refused (read and act tools).
- always_ask: every call is its own approval; never auto-approved (auto_approve lists don't apply). A person at the
  CLI or dashboard still runs it directly, except for the hard-floor tools below.
- ask: the default for act tools; one approval per call (or covered by a run grant).
- auto_clean: runs without approval while the run is clean (hasn't read untrusted content).
- auto: runs without approval at any time, even after the run read untrusted content (external context). For an
  egress tool it also lifts the "no egress after untrusted content" refusal, but only while the run holds none of
  the owner's private data (Caller.sensitive). Meant for low-risk tools whose worst case is harmless (skipping a
  track, searching a shop). Durable-state tools (writes_state) keep their laundering guard at every level.

Every tool belongs to a group (`GROUPS`): the connector's group unless the tool declares its own. A group ships a
default level; a tool may ship its own level instead (an override), else it takes the group's.

Precedence, strongest first:
1. hard floor in code: pmjc_shell_run and the settings tools are always_ask and need approval even at the CLI;
2. the owner's `tool_levels` in settings.yaml (exact tool name, else the longest matching glob);
3. the owner's `auto_approve` lists (global and an owner-written job's own): at least auto_clean (auto stays auto);
4. a level the tool declares in code (`@tool(level=...)`) that is stricter than its group's shipped default: it
   survives a looser `group_levels` (setting `music: auto` doesn't open playlist writes; only `tool_levels` can);
5. the owner's `group_levels` in settings.yaml (the tool's group);
6. the level the tool declares in code;
7. its group's shipped default (`GROUPS`), else `ask`.
`auto` applies in full only to tools without egress or with `fixed_host` egress; any other egress tool set to `auto`
runs as `auto_clean` (`for_tool`), so a glob like `*: auto` can't open web_fetch or the browser after taint.
Levels other than `off` and `auto` only change anything for act tools; `auto` also matters for egress read tools.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

LEVELS = ("off", "always_ask", "ask", "auto_clean", "auto")
_RANK = {lv: i for i, lv in enumerate(LEVELS)}  # strictest first

# Shipped groups: default level and what belongs there. Unknown groups (a plugin's, a mounted MCP server's) default
# to `ask`. Keep low-risk groups (`auto`) to tools whose worst case under prompt injection is harmless.
GROUPS: dict[str, dict[str, str]] = {
    "music": {"level": "auto", "about": "music playback and search (Spotify): skip, pause, queue, play, search"},
    "shopping": {"level": "auto", "about": "searching shops and listings (eBay, Craigslist); never buys"},
    "local_info": {"level": "auto", "about": "weather, places and geocoding from public services"},
    "web": {"level": "ask", "about": "web search, page fetch and research (a fetched URL can carry data out)"},
    "browser": {"level": "ask", "about": "the signed-in browser profile"},
    "social": {"level": "ask", "about": "chat and messaging (Discord, Telegram, Slack); sends always ask"},
    "notify": {"level": "ask", "about": "pmjc's own notification channels (ntfy, webhooks)"},
    "email_calendar": {"level": "ask", "about": "Gmail and Google Calendar"},
    "home": {"level": "ask", "about": "smart-home devices (Home Assistant)"},
    "files": {"level": "ask", "about": "local files (read-only) and desktop notifications"},
    "system": {"level": "ask", "about": "this PC, remote machines (SSH), local models (LM Studio)"},
    "dev": {"level": "ask", "about": "developer services (GitHub)"},
    "pmjc": {"level": "ask", "about": "pmjc itself: context, memory, threads, jobs, watches, settings, commands"},
    "commands": {"level": "ask", "about": "owner-defined command tools"},
    "mcp": {"level": "ask", "about": "tools of mounted MCP servers"},
}


def hard_floor(name: str) -> bool:
    from . import settings_edit, shell
    return name == shell.TOOL or name in settings_edit.TOOLS


def configured(name: str, tool_levels: dict[str, str]) -> str | None:
    """The owner's level for `name` from `tool_levels`: an exact key wins, else the longest matching glob."""
    if name in tool_levels:
        return tool_levels[name]
    globs = [k for k in tool_levels if any(ch in k for ch in "*?[") and fnmatchcase(name, k)]
    return tool_levels[max(globs, key=len)] if globs else None


def group_default(group: str | None) -> str | None:
    return GROUPS.get(group or "", {}).get("level")


def effective(name: str, default: str | None, tool_levels: dict[str, str], auto_approve: set[str] | list[str] = (),
              group: str | None = None, group_levels: dict[str, str] | None = None) -> str:
    """The level that applies to tool `name` (see the precedence above). `default` is the level the tool declares,
    `auto_approve` the union of the lists that apply to this caller (global settings plus an owner-written job's own),
    `group` the tool's group and `group_levels` the owner's per-group settings."""
    if hard_floor(name):
        return "off" if configured(name, tool_levels) == "off" else "always_ask"
    own = configured(name, tool_levels)
    if own:
        return own
    shipped_group = group_default(group) or "ask"
    owner_group = (group_levels or {}).get(group or "")
    if default and _RANK.get(default, 2) < _RANK[shipped_group]:  # a stricter per-tool override survives the group
        rest = min(owner_group or default, default, key=lambda lv: _RANK.get(lv, 2))
    else:
        rest = owner_group or default or shipped_group
    if name in auto_approve and rest != "auto" and rest != "off":
        return "auto_clean"
    return rest


def for_tool(t, settings, auto_approve=()) -> str:
    """`effective` for a registry ToolSpec under the loaded settings, plus a caller's extra auto_approve list."""
    lv = effective(t.name, t.level, settings.tool_levels, set(settings.auto_approve) | set(auto_approve),
                   getattr(t, "group", None), getattr(settings, "group_levels", None))
    if lv == "auto" and getattr(t, "egress", False) and not getattr(t, "fixed_host", False):
        return "auto_clean"
    return lv


def harmless_groups(settings) -> set[str]:
    """Groups whose effective group level is `auto`: their data is low-risk, so it may ride into another auto tool."""
    own = getattr(settings, "group_levels", None) or {}
    names = set(GROUPS) | set(own)
    return {g for g in names if (own.get(g) or group_default(g)) == "auto"}
