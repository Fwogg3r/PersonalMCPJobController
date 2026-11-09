---
title: "Front-door routing: tiers, route rules, sticky provider per thread, routing log, answer footer"
status: active
links: [phase4/x, phase4/w, phase4/z]
---
Decided 2026-11-09 after an Opus review: see DECISIONS.md "Front-door routing". First shippable version: the router fixes (gpu_busy ignores LM Studio itself; interactive requests do not block on starting a local host), `tiers` + `route_rules` config resolved into the provider order with prefix/model-id overrides, a sticky provider per thread, a routing log, and an answer footer stripped from incoming history. Then user-driven escalation, the executor-enforced act ledger, model-invoked escalation, and triage only if evals show it helps.

## Notes

Progress (build steps 1-5 of the decision done; steps 6-10 remain: user-driven escalation, executor-enforced act ledger, model-invoked escalation, triage only if evals show it helps, and tuning from the log):
- Router fixes: `gpu_busy` ignores processes matching `router.gpu_ignore_processes` (nvidia-smi compute apps); interactive requests (`Need.interactive`) use `healthy(start=False)`, start a down local host in a deduplicated background task and move on; jobs and watches keep the blocking start.
- `router.tiers` / `router.route_rules` (new `src/pmjc/routing.py`): built-in tiers derived from the providers, first-match rules (prefix, keywords, surface, min_chars, has_image placeholder), `pmjc:<tier>` model ids in `/v1`, a re-check that routing never widens `router.candidates()`.
- Sticky provider per thread (`thread_route` table); a prefix or model id replaces it.
- `routing` table plus `pmjc routing --days N` and a Routing-tab section; `retry_of` marks a prefix re-ask of the previous turn.
- Answer footer (`router.footer`, Discord and `/v1` by default), stripped from thread history and from the `/v1` conversation key.
- Also fixed: `ask()` reused its `private` variable for the history "sensitive" flag, so any continued thread was forced to local models.
