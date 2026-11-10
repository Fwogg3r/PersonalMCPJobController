---
title: "Docs: register the stdio MCP server with uv run --no-sync"
status: done
links: []
---
Notes: 2026-11-09 the desktop pmjc MCP server failed with 'Connection closed': uv run re-synced after a merge and couldn't replace .venv/Scripts/pmjc.exe, which older pmjc mcp processes held open (Windows). README and AGENTS.md now use --no-sync; the owner's user-scope entry was re-registered the same way and its handshake checked.
