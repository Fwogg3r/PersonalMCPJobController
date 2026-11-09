---
title: "Shopping sources: eBay Browse API and Craigslist search connectors"
nickname: Shopping
status: active
links: [phase4/u]
---
**What:** Structured shopping sources: an eBay Browse API connector (search with price/condition/buying-option filters, item quantity, returns, links) and a Craigslist search connector (the `sapi.craigslist.org/web/v8/postings/search/full` feed: price, title, area, link).

**Why:** Listing watches are more reliable from structured data than from scraped pages, and any model, local included, can filter JSON rows. eBay's API also avoids bot blocking.

**How:** eBay developer app key in the credential store (owner creates it); read-scope tools returning compact rows. Craigslist link = `https://www.craigslist.org/view/d/<slug>/<code>` from item fields 6 and 13.

**Notes:**

- Progress 2026-11-09: the eBay connector is built (`connectors/ebay.py`, tool `ebay_search`: up to 10 queries per call, optional `details` for quantity and item specifics, `min_quantity` filter; client-credentials token cached; config `connectors.ebay.marketplace` / `sandbox`; credentials `ebay.client_id` and `ebay.client_secret` via `pmjc auth set ebay`; tests mock the API). Craigslist is still to do. The eBay API details are unverified against the live service.

Note (2026-11-09): not yet tried against the live API; the owner is waiting for eBay to approve the developer account, which should take about a day. First live check: the quantity field (estimatedAvailabilities) and the conditions filter.
