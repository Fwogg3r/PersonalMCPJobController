---
title: "Claude with Chrome: router lets Claude runs browse like Claude Code"
nickname: Claude+Chrome
status: planned
links: [phase7/f, phase7/d]
---
**What:** Runs routed to Claude (the `claude_cli` provider) can use Claude in Chrome: open tabs in the owner's real Chrome, read pages with links, scroll, click and use existing sign-ins, the way Claude Code does.

**Why:** Today `_claude_cli` in `src/pmjc/agent.py` locks the CLI to `--tools "" --strict-mcp-config --allowedTools mcp__pmjc`, so even a Claude run gets only `browser_read` (headless, text only, one page per run). Tasks like the server-build deal watch (eBay + Craigslist + Facebook Marketplace, with item links) had to run as a Claude Code scheduled task instead of a pmjc job.

**How:**
- Job/watch option `browser: chrome` (default off) plus `browser_sites: [...]` allowlist. When set, the router prefers a `claude_cli` provider and starts the CLI with Chrome enabled and the Chrome tools added to `--allowedTools`, still under `--strict-mcp-config`.
- Router rule: a run that needs interactive browsing goes to Claude+Chrome when available; otherwise it falls back to the upgraded pmjc browser on a local model (see the browser-upgrade item), and says so in the result.
- Safety: Chrome reads are untrusted content, so the dual-LLM rule still applies: anything that sends, posts, buys or submits needs approval; results leave only through notify channels. Never steal focus: work in one new tab and close it.
- Verify which CLI flag/connector enables Chrome in non-interactive (`-p`) mode before building.

**Notes:** Requested 2026-11-08 after the deal-watch setup. Design debate (Opus/Fable) before building.
