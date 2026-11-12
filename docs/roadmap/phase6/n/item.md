---
title: "Thinking effort per request: rules plus one escalation, /fast and /think, effort hints per connector"
status: active
links: [phase6/m, phase6/j]
---
**What:** a provider `thinking` block (`control: none | param | kwargs`, `policy: fixed | adaptive`, `default`, `adaptive_floor`, `escalate`, `levels`), `router.effort_prefixes` (/fast off, /think high), connector `effort_hint` and owner `router.connector_effort`. Adaptive picks the level per run by rules and raises it once on a failure signal. Code: src/pmjc/effort.py; the call log has an `effort` column; eval reports show effort, escalations, reasoning tokens and median latency. Off by default.

**Why:** the local model's template defaults to xhigh thinking, so every request thought at maximum. Decision: DECISIONS.md, 'Thinking effort per request...'; review: docs/reviews/2026-11-11-thinking-effort-smoke.md.

**Measured 2026-11-11:** LM Studio ignores chat_template_kwargs and accepts a top-level reasoning_effort (none, minimal, low, medium, high, xhigh). Reasoning tokens on a coding prompt: none 0, low 113, medium 555, high 404, xhigh 308-550 (noisy).

**Benchmarked 2026-11-11 (docs/reviews/2026-11-11-thinking-benchmark.md):** every level on 78 smoke prompts and the basic and multistep evals (696 runs), then off against a lean adaptive mapping (232 runs). On the local 27B model, thinking didn't improve tool use. Fixed levels scored 0.80-0.84 / 0.91-0.93 against off's 0.844 / 0.956, and adaptive 0.857 / 0.965, all within noise. Median latency was 1.6-3x off's. Lean adaptive scored 0.868 / 0.930 against off's 0.893 / 0.963 with twice the p90. Recommendation for this model: `policy: fixed, default: off`, with `/think` for hard requests.

**Still to do:** a coding and investigation smoke set (neither suite has one), then decide whether build requests should think; maybe a per-turn on/off toggle (measure the cache cost first); a reasoning-token budget per level.

**Built in the same change (loop speed-ups from the review):** in-process runs skip the pmjc_context / pmjc_thread_get preamble; the core tool set went from 22 to 11 (thread, watch and job admin load with the pmjc connector, now aliased by watch, reminder, thread, settings ...; tool payload for "skip this song" 3953 -> 3185 tokens); parallel plain reads (never with egress or state writes); a note on repeated identical calls; a "last model call, answer now" note; one retry of LM Studio's transient "fetch failed" engine error.
