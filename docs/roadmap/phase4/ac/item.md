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
- Integration fixes (2026-11-09): `gpu_busy` no longer relies on nvidia-smi compute apps (games are graphics apps). Busy = a process in `router.gpu_busy_processes` (default empty) is running, or utilization at or over the threshold while pmjc has no local request in flight (in-flight counter in `agent.py` around local OpenAI-compatible calls); high utilization with pmjc's own request in flight is not busy. Robust without nvidia-smi (psutil, else tasklist or ps).
- GPU busy now asks (owner decision): an interactive request whose first choice is a guarded local provider returns a notice (what uses the GPU, utilization) with use local anyway (approval of the internal `pmjc_route_override`), use a cloud model (not for private threads, only if a cloud candidate exists) or wait; choosing re-runs the request in the same thread via the approvals continuation (Discord buttons, `/v1` signed links, REST `route_choice` field). `router.gpu_busy_action: ask | cloud | local` (default ask); jobs and watches `router.gpu_busy_jobs: wait | cloud | local` (default wait: a job run is recorded `deferred` and retried on its next schedule; a watch retries after `gpu_busy_backoff_seconds`). Private work never goes to the cloud. Choices are logged in the routing log (`decision` column).
- Sticky provider stores planned and actual provider plus fallback reason. After a fallback it stays on the fallback when the thread is hard (strong tier, `stay_tool_calls` tool calls, condensed, long history) and otherwise switches back to the planned provider when it is ready; the footer states which and offers a pivot (`/local`, `/strong`); decisions are in the routing log; a private thread never sticks to the cloud.
- Found by the cross-feature test: the research sub-run was given tool_select briefs (its web tools can end up unloaded in `tool_mode: select`); fixed, quarantined callers always get their tools in full.
