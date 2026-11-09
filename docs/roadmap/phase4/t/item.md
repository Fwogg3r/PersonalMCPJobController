---
title: "Shopping sources: eBay Browse API and Craigslist search connectors"
nickname: Shopping
status: idea
links: [phase4/u]
---
**What:** Structured shopping sources: an eBay Browse API connector (search with price/condition/buying-option filters, item quantity, returns, links) and a Craigslist search connector (the `sapi.craigslist.org/web/v8/postings/search/full` feed: price, title, area, link).

**Why:** Listing watches are more reliable from structured data than from scraped pages, and any model, local included, can filter JSON rows. eBay's API also avoids bot blocking.

**How:** eBay developer app key in the credential store (owner creates it); read-scope tools returning compact rows. Craigslist link = `https://www.craigslist.org/view/d/<slug>/<code>` from item fields 6 and 13.
