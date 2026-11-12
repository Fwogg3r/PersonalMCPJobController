---
title: Smoke test of every real tool with the local model (reads live, acts recorded)
status: active
links: [phase6/i, phase6/n]
---
**What:** `pmjc eval smoke -p <local provider>` (src/pmjc/evals/smoke.py): one plain request per tool from src/pmjc/evals/smoke/<connector>.yaml (65 generic prompts across 12 connectors) plus the owner's own prompts in <data folder>/evals/smoke/. Reads run for real; act tools and state writers are swapped for recorders in a registry only the smoke module builds; the executor is unchanged. The temporary data folder copies settings without notify channels, the Discord door, approval follow-ups, jobs, monitoring and grants, copies credentials and writes rotated OAuth tokens back. Local providers only. `--coverage` lists tools without a prompt. New task check: `tools_called_any`.

**Why:** the eval uses fake connectors; the owner wants every real tool exercised with the local model, to tune tool use and context.

**Fixes after the first full runs (2026-11-11):** the temporary home now copies the owner's `connectors.d/` and `plugins/`; before that, the owner's own tools were missing and their 13 prompts measured nothing. Prompts that needed a song playing now name a song. The geocode prompt uses a plain address (the model knew the Eiffel Tower's coordinates without a tool). Research gets 6 turns. With thinking off the score went from 0.844 to 0.893 (owner prompts 0.58 to 0.73, places 0.25 to 1.00). Still failing: the taint cases (phase8/q), Spotify unsave and transfer, playlist create over its turn limit, and the dentist handoff (the model asks for details first).
