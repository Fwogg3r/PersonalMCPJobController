---
title: "Discord front door: chat with pmjc through your own bot"
status: active
links: [phase3/j]
---
Gateway listener in the service (outbound websocket, no tunnel). Owner ids only; a message in a door channel starts a Discord thread mapped to a pmjc thread; DMs too (new resets). ask() gained history_turns (last exchanges in the prompt; untrusted history taints the run). Act tools post Approve/Deny buttons that only owners can press; Discord.notify uses buttons while the door is connected. CLI: pmjc discord-door setup/status; GET /api/discord-door. 7 tests in tests/test_discord_door.py (fake REST, fake model, local fake gateway). Not yet verified against real Discord.
