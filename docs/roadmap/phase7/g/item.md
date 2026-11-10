---
title: "Listing watch without a model: structured queries, rules, NEW marks, chunked alerts"
nickname: Listing watch
status: planned
links: [phase7/e, phase7/f]
---
**What:** A job type that runs fixed structured queries (eBay `ebay_search`, Craigslist `craigslist_search`) and applies rules written by the user: price caps, title include/exclude terms, conditions, minimum quantity. It tracks first-seen dates per listing, marks NEW matches, sorts cheapest first and posts every match to the notify channel, split into chunks under the chat message limit. No model is involved.

**Why:** Cheaper, faster and more predictable than an LLM reading listings, and taint stops mattering because no model reads third-party text. Covers most of phase7/f; only sites without a structured source (Facebook Marketplace) still need a browser (phase7/c).

**How:** A `listing_watch` job kind in the job file (queries + rules + message template); seen-state in the db with a retention window; reuse the notify path and chunking. Generic example job in `jobs examples`; the owner's targets stay in their own job file.

**Notes:** Proposed 2026-11-10 after the eBay live check (phase7/e).
