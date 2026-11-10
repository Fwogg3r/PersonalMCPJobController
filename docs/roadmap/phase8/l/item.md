---
title: Per-tool permission levels (off, always_ask, ask, auto_clean) with shipped defaults
status: done
links: []
---
Levels per tool: off, always_ask, ask (default for act), auto_clean (alias for today's auto_approve). Defaults in @tool(level=...), owner overrides in config tool_levels (hard-denied from chat settings tools). Design and review: DECISIONS.md "Run grants and permission levels", docs/reviews/2026-11-10-run-grants.md.

Outcome (2026-11-10): built. `pmjc.levels` (precedence: shell/settings hard floor > `tool_levels` exact name or longest glob > the owner's `auto_approve` lists > shipped `@tool(level=...)` / YAML `level:` > ask). `off` hides the tool from every list and refuses it. Shipped always_ask: discord_send, discord_dm, telegram_send, slack_send, github_comment, browser_act, shell, settings; shipped auto_clean: lmstudio_load, lmstudio_unload, command_stop, gmail_draft. `tool_levels` hard-denied from chat settings tools; `/api/tools` reports the effective level. 5 new tests in tests/test_core.py; test_commands updated (command_stop is auto_clean). Not built: a level that writes notes after taint without the untrusted label (see DECISIONS; open question for phase8/m).
