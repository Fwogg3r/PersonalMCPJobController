---
title: "Chat commands on /v1: tools, connectors, approvals, status, model, threads"
status: done
links: []
---

## Notes

- Chat commands on `/v1` (`src/pmjc/chat_commands.py`, wired in `openai_door.py`): `/help /tools [filter] /connectors /approvals /status /model [tier|provider|auto] /threads /new`. Answered by pmjc with no model run; same auth as `/v1`; output filtered by the token's scopes; long lists cap at 60 lines.
- `/strong` and `/local` stay routing prefixes; unknown `/foo` goes to the model. Names live in `chat_commands.COMMANDS`; `front_door.chat_commands: false` turns them off.
- `/model` writes the thread's sticky route (`thread_route`), never widens (private threads: local providers only). Commands are stored as `command` thread items, which `ask.history()` skips.
- Tests: `tests/test_chat_commands.py`.
