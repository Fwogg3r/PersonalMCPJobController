---
title: "Front-door routing: tiers, route rules, sticky provider per thread, routing log, answer footer"
status: planned
links: [phase4/x, phase4/w, phase4/z]
---
Decided 2026-11-09 after an Opus review: see DECISIONS.md "Front-door routing". First shippable version: the router fixes (gpu_busy ignores LM Studio itself; interactive requests do not block on starting a local host), `tiers` + `route_rules` config resolved into the provider order with prefix/model-id overrides, a sticky provider per thread, a routing log, and an answer footer stripped from incoming history. Then user-driven escalation, the executor-enforced act ledger, model-invoked escalation, and triage only if evals show it helps.
