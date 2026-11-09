---
title: "Taint hardening: approval for writers after untrusted content, output defanging, derived sensitive flag"
status: done
links: []
---
Steps 1-3 of the decision "Multi-step work on local models" (DECISIONS.md, 2026-11-09). Found in the Opus review: pmjc_memory_set, pmjc_contact_set, pmjc_job_create and pmjc_watch_start are jobs-scope (not act), so a run that read untrusted content could write attacker text into memory that lands in every later system prompt.

**Progress 2026-11-09 (steps 1-3 done):**
- **Writers:** new `@tool(..., writes_state=True)` flag, enforced in the executor. Flagged: `pmjc_memory_set`, `pmjc_memory_delete`, `pmjc_contact_set`, `pmjc_job_create`, `pmjc_job_delete`, `pmjc_watch_start`. A tainted run gets an approval request instead of the call; an approved write runs as a tainted read-scope caller, so it is stored untrusted (a job it creates can't act, a watch it starts is read-only). Thread start/note already store untrusted items for tainted callers, so they aren't flagged. Clean runs behave as before.
- **Declared reads:** `read_sites` never matches a URL with a query string, `read_urls` are exact, new `read_prefixes` (jobs and watch specs, owner-only like the other two) match scheme + host + path prefix.
- **Defang:** for tainted runs, links to untrusted hosts in answers, job and watch results and approval previews become `` `hxxps://host[.]example/path` `` (no query, fragment or userinfo). Allowed: settings `output_link_allowlist` (empty by default) and hosts of URLs the run's tools returned, with queries kept only if verbatim in a tool result. Discord answers from tainted runs carry SUPPRESS_EMBEDS; notifications and approval messages always do. The dashboard already inserted answers as text; it now also has a CSP that blocks remote images.
- **Sensitive:** `Caller.sensitive`, derived and fail-closed; tools get `private_output` (default true; false on web_search, web_fetch, browser_read/extract/browse, craigslist_search, ebay_search, weather_forecast, places_nearby, places_geocode). System block, thread history and approval-result text set it at the start. Exposed in audit rows (`ok` and `run_end`: `untrusted=0 sensitive=1`), ask results, job run results and `ask.history()` (now returns text, untrusted, sensitive). No behaviour depends on it yet; step 4 (research sub-run) will use it.
- Tests: `tests/test_taint_hardening.py` plus two Discord door tests. Decision recorded in DECISIONS.md (phase4/ab).
