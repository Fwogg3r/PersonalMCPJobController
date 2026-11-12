# Review 2026-11-11: adaptive thinking effort, a smoke test of every tool, speed levers for the local model

Reviewer: an Opus subagent, run on the plan (read-only). Input: the owner's goal (generic agentic work from one chat session, fast and high quality on the local Qwen3.8 27B, with thinking effort managed per task), the multistep eval results (phase6/h, phase6/m), and the code (agent loop, routing, tool selection, executor, evals).

## Findings and recommendations (summary of the review)

### 1. Thinking effort
- The model's chat template takes `enable_thinking` and `reasoning_effort` (low, medium, xhigh; default **xhigh**), so every local run thinks at maximum today.
- Effort written into the system message changes the start of the prompt: switching level costs one full prompt re-read (prompt-cache miss). Turning thinking off changes only the end of the prompt (generation prompt), so toggling on/off is cache-safe.
- Ranked: (a) rules at the start of a request (prefix `/fast` `/think`, route tier, effort hints of the selected connectors, request length and clauses, multi-connector), then (e) escalation on failure signals (tool error, denied call, unsent or unknown tool, repeated `pmjc_capabilities`, repeated identical call, bad JSON arguments), at most once per run and logged; then (b) a per-turn on/off toggle only (off after a simple successful result, on after a failure). Rejected: (c) a triage call (evicts the single cache slot; no better than rules) and (d) model self-selection (it has to think to decide).
- Optional: a reasoning-token budget per level, by cancelling the stream and re-sending with the thinking so far closed (`</think>`), if LM Studio continues a final assistant message.
- Config: default off (today's behaviour), provider `thinking: {control, levels, policy: fixed|adaptive, default, off_after_simple_success, max_reasoning_tokens}`, `router.effort_rules` (same matcher as route_rules), connector `effort_hint`, owner `connector_effort` overrides.
- Measure with eval variants (fixed-high, fixed-medium, adaptive), n >= 5; adopt adaptive if its score is within ~0.03 of fixed-high and median latency drops >= 30%.

### 2. Smoke test of every real tool
- A separate `pmjc eval smoke` that builds a registry where act tools' functions are replaced by recorders (validate arguments, return a stub). The executor is unchanged (scopes, taint, levels, grants, approvals still apply); approvals are decided automatically in an isolated home with no notify channels. Guards: refuse to install on the real home or inside a running server; a source-scan test limits construction to the smoke module.
- Modes: `--fakes` (no credentials) and `--live-reads` (copies only the named connectors' credentials into the temporary home; per-tool `smoke: live | fake | skip` opt-in). MCP-mount tools are dry-run unless the owner's overrides mark them read.
- Prompts: curated YAML per built-in connector (generic, placeholders filled from config at run time), a coverage check, chained prompts for tools that need ids; mounts get generated prompts in the private data folder.
- Score per tool: right tool first and calls wasted before it, valid arguments, act proposed and approval requested (never executed), whether tool selection had the connector up front, turns, tokens (prompt, completion, reasoning, cached), time to first token, wall time, effort and escalations. Report the worst tools first.

### 3. Other levers, ranked
1. Drop the per-run preamble: in-process runs already get model.md and thread history, yet rule 1 makes the model call `pmjc_context` (and often `pmjc_thread_get`), costing one or two model calls per run.
2. Thinking effort (above).
3. A fast path for short single-connector requests (that connector plus a few core tools, low/off effort, max_turns 3, escalate on failure).
4. Cut the core tools from ~23 to ~8; move jobs, watch admin, memory and contact writers and thread admin behind keyword selection.
5. Loads that don't change `tools` (return the compact schema in the loader result; unsent calls already run through the executor), if LM Studio accepts tool calls not in `tools`.
6. Parallel read calls from one response (acts stay sequential).
7. Stagnation detection and a "two turns left, answer now" note.
8. Compact schemas by default for local providers, once measured.
9. Keep system-prompt bytes stable (dynamic text last).
10. Try keeping `reasoning_content` within the current tool chain (an eval variant).

### Risks
- LM Studio may not forward the effort parameter (then on/off by prefill, levels by system-prompt text, one cache miss per change).
- Low effort can pick a wrong act confidently; approval is the backstop.
- Live-read smoke runs touch real services; per-tool opt-in.
- The dry-run registry is a new trust boundary; it needs the guards above.
- Eval noise: n=3 on 8 tasks can't separate gains under ~0.1.
