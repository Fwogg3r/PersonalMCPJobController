---
title: "Discord front door: chat with pmjc through your own bot"
status: done
links: [phase3/j]
---
Gateway listener in the service (outbound websocket, no tunnel). Owner ids only; a message in a door channel starts a Discord thread mapped to a pmjc thread; DMs too (new resets). ask() gained history_turns (last exchanges in the prompt; untrusted history taints the run). Act tools post Approve/Deny buttons that only owners can press; Discord.notify uses buttons while the door is connected. CLI: pmjc discord-door setup/status; GET /api/discord-door. 7 tests in tests/test_discord_door.py (fake REST, fake model, local fake gateway). Not yet verified against real Discord.

v2: threads under door channels (any thread whose parent is a door channel binds on first message; THREAD_DELETE closes), slash commands `/tool` and `/pmjc` (name set by `discord_door.command_name`) (new/close/delete/trace/model/private/autoapprove/jobs/watches/notify/switch/friend/status) registered as guild commands, per-thread prefs table, `Caller.on_tool` tool trace, `ask(extra_system=)` friends note. 8 new tests (15 in the file). Not verified against real Discord; enable/disable of jobs reuses the REST toggle logic.

Verified live (2026-11-07): gateway connects, guild commands register, a thread created in the Discord UI is answered, `/pmjc status` works. Fixed after the live run: system messages (e.g. "started a thread" notices) are ignored. Setup steps written up in docs/REMOTE.md sections 3 and 6, including the Google OAuth client and the claude.ai connector, which were also verified end to end.

Added after the live run: provider fallbacks are logged and shown in the reply footer, and a per-run log channel (`discord_door.log_channel_id`, `discord-door setup --log-channel`) that gets each run's pmjc log lines.
