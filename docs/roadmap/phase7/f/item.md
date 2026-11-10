---
title: Deal watch as a pmjc job (retire the Claude scheduled task)
nickname: Deal watch
status: planned
links: [phase7/g]
---
**What:** Run a recurring listing watch (deal watch) as a pmjc job: every morning, search marketplaces (e.g. eBay, Craigslist, Facebook Marketplace) for items under price thresholds the user sets, list every match with links, mark NEW ones, and split long results across chat messages. The targets and thresholds live in the user's own job file, never in the repo.

**Why:** It currently runs as a Claude Code scheduled task that drives Chrome and relays through `pmjc_watch_start` (type=time, respond=raw) to the Discord notify channel, because pmjc jobs couldn't browse.

**How:** A pmjc job using the Claude+Chrome mode, falling back to the upgraded browser and shopping connectors on a local model; then retire the scheduled task.

Note (2026-11-10): most of this no longer needs a browser or a model. eBay (live-checked) and Craigslist have structured connectors, so the listing watch without a model (phase7/g) covers those two sites; only Facebook Marketplace still needs Claude+Chrome (phase7/c). Plan: build phase7/g, move the eBay and Craigslist parts of the scheduled task to it, keep Chrome only for Facebook until phase7/c lands.
