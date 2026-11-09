---
title: "Browser upgrade: real Chrome, links, interaction and multi-page reads for local models"
nickname: Browser v2
status: planned
links: [phase4/u]
---
**What:** Upgrade pmjc's own browser connector so local and cheaper models can do real browsing work without Claude.

**Why:** On 2026-11-08 a test job showed eBay returns an error page to the headless Playwright profile; `browser_read` returns visible text only (no hrefs, so no listing links); and a run can read only one page, because `browser_read` is `egress=True` and the executor blocks egress after untrusted content.

**How:**
- Visible real Chrome (`channel="chrome"`, headed) on the rightmost monitor, never focused, with the existing persistent profile; fewer bot blocks than headless Chromium.
- `browser_read` returns `links` (text + absolute href) and optional CSS-selector extraction (`browser_extract(url, selector, fields)`) so small models get structured rows instead of a page dump.
- Interaction tools: scroll, click, type, wait-for; anything that submits a form or buys stays an `act` tool with approval.
- Multi-page reads: a job may declare `read_urls`/`read_sites` up front; reads matching that declared list don't count as egress after untrusted content (the URL was fixed by the owner, not the model). Everything else keeps the current rule.
- Covers the interactive part of phase4/l and runs in its own process (phase4/c).

**Notes:** Settings used for the first test: `connectors.browser.allowed_domains` now includes `ebay.com` and `craigslist.org` in the owner's settings.yaml.
