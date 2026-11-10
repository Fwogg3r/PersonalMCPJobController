---
title: CLI usable while the service runs (no os error 32 from uv re-sync)
nickname: CLI lock
status: planned
links: []
---
**What:** Make the CLI usable while the pmjc service or MCP server is running.

**Why:** On Windows a running pmjc locks `.venv/Scripts/pmjc.exe`, so `uv run pmjc ...` fails with "os error 32" when uv tries to re-sync. Workaround today: `uv run --no-sync pmjc ...`.

**How:** Options to weigh: run the service from an installed copy (`uv tool install`) so the dev venv is never locked; or document `--no-sync` everywhere and have `pmjc doctor` detect the lock and print the fix. Pick one, update AGENTS.md and the docs.
