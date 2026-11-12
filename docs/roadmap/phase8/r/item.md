---
title: "Tool groups and the auto level: low-risk groups (music, shopping, local info) run at any time"
status: done
links: [phase8/l]
---
**Owner request (2026-11-12):** low-risk tools (Spotify, shop searches) should run without approval at any time, even after the run read external content; group tools with defaults a tool can override.

**Built:** `pmjc.levels.GROUPS` (music, shopping, local_info at `auto`; web, browser, social, notify, email_calendar, home, files, system, dev, pmjc, commands, mcp at `ask`), connector `group` and `@tool(group=..., fixed_host=...)`, YAML `group:`/`fixed_host:`, the fifth level `auto`, owner `group_levels` (hard-denied from chat). `auto` skips approval and the repeat re-ask after taint; fixed-host egress tools at `auto` may run after taint unless the run holds private data from a non-auto group (`Caller.sensitive_from`), capped at 30 per run. A shipped tool level stricter than its group survives a looser group setting (playlist writes stay `ask`). Design reviewed by an Opus-level subagent: [review](../../../reviews/2026-11-12-tool-groups.md), DECISIONS.md "Tool groups and the auto level".

**Outcome:** "what's playing? skip it and queue X" now runs with no prompt; ebay/craigslist/weather/places searches work after a run read outside content. Playlist writes after reading are still refused (phase8/q stays open).
