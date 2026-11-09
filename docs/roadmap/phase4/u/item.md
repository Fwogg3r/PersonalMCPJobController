---
title: Deal watch as a pmjc job (retire the Claude scheduled task)
nickname: Deal watch
status: planned
links: []
---
**What:** Move the server-build deal watch into pmjc: every morning, alert on any RTX 3090 Buy It Now at or under $1,200 and any listing with 8+ matching 32GB DDR4-2666+ RDIMMs at or under $140/stick (eBay, Craigslist Boston/NH/Maine, Facebook Marketplace), listing every match with links, NEW marked, split across Discord messages when long.

**Why:** It currently runs as a Claude Code scheduled task (`server-build-deal-watch`) that drives Chrome and relays through `pmjc_watch_start` (type=time, respond=raw) to the Discord notify channel, because pmjc jobs couldn't browse.

**How:** A pmjc job using the Claude+Chrome mode, falling back to the upgraded browser and shopping connectors on a local model; then retire the scheduled task.
