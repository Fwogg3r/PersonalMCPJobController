---
title: Owner-only notify after untrusted reads in MCP client sessions
nickname: Notify after taint
status: idea
links: [phase7/f]
---
**What:** Let an MCP client session that has read third-party content (e.g. `ebay_search`) still send its result to the owner's own notify channel, the way pmjc's own job and watch runs may.

**Why:** pmjc's MCP tools say a session that reads third-party content can't send data out. So a Claude Code scheduled task can't use `ebay_search` and then alert the owner through `pmjc_watch_start` in the same run; the deal watch keeps scraping eBay through Chrome instead.

**How:** Design debate first (taint rules are the security core): an owner-bound notify tool that only reaches configured owner channels, never arbitrary recipients, size-capped and defanged. Verify the current MCP-session behavior before building.
