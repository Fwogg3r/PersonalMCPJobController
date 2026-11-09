---
title: Deal watch as a pmjc job (retire the Claude scheduled task)
nickname: Deal watch
status: planned
links: []
---
**What:** Run a recurring listing watch (deal watch) as a pmjc job: every morning, search marketplaces (e.g. eBay, Craigslist, Facebook Marketplace) for items under price thresholds the user sets, list every match with links, mark NEW ones, and split long results across chat messages. The targets and thresholds live in the user's own job file, never in the repo.

**Why:** It currently runs as a Claude Code scheduled task that drives Chrome and relays through `pmjc_watch_start` (type=time, respond=raw) to the Discord notify channel, because pmjc jobs couldn't browse.

**How:** A pmjc job using the Claude+Chrome mode, falling back to the upgraded browser and shopping connectors on a local model; then retire the scheduled task.
