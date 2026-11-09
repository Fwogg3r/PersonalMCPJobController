---
title: "Auto-condense: keep local-model runs going near the context limit, escalate with approval"
status: active
links: [phase4/j]
---
Goal: get more out of 32k-context local models. Where the room goes: system prompt + model.md + tool schemas take a large fixed share (measure it per model), then the in-run tool loop (up to 12 turns of calls and results) grows fastest. Conversation history across messages is already bounded (history_turns, 2000 chars per item), so condensing mainly matters inside a run.

Plan:
- Track tokens per run (provider usage, or estimate ~4 chars/token) against the loaded model's context (LM Studio reports it).
- At ~75% full, condense: the same local model rewrites older tool results and turns into a short state summary (facts found, actions done, what's left). It keeps the latest turn, pending approvals and the act ledger word for word. Large single tool results are cut or summarized as they arrive, not after.
- Trust carries over: a summary of untrusted content stays untrusted (the run stays tainted); approvals are never summarized away.
- If it still won't fit (or condensing fails twice), offer to hand the run to an external model (Claude) through an approval card showing the estimated size and cost. Private threads never leave local models; they stop with a clear "too long" message and the summary so far.
- Log each condense (before/after tokens) to the run log and #logs.
- Also: a cap on fixed overhead (send only relevant tool schemas to small-context models) may buy more room than condensing.

Open: thresholds per model, and whether the condensed summary is written back to the pmjc thread so later messages start from it.

**Notes:**
- **Progress 2026-11-09 (steps built: result caps, paging, condense pass; escalation card not started):**
  - Result caps: provider option `max_result_chars` (default derived from `context_tokens`: 4000 chars up to 32k, rising linearly to the old 20000 at 128k and above; an explicit value wins). A longer result is cut and the full text is stored under a short handle (`r_xxxx`, table `results`); the model sees `[truncated: N more chars; call pmjc_result_get(id="r_xxxx", offset=4000)]`. Applies to the OpenAI-compatible and Anthropic loops.
  - `pmjc_result_get(id, offset=0, max_chars=None)`: read scope, not egress. Handles belong to the thread (or, without a thread, the run) that made them, so another thread can't read them, and expire after the setting `result_ttl_hours` (default 24). The handle records whether the run was tainted or the tool's output untrusted; reading a slice of such a result taints the reading run the same way. Runs with tools get the tool automatically.
  - Condense pass (OpenAI-compatible loop): provider options `condense_at` (default 0.7) and `reserve_tokens` (default 2048). Used tokens come from `usage.prompt_tokens` (else chars/4). Above `condense_at` of (context minus reserve), one tool-less completion on the same provider turns the older turns into a state summary. The system prompt and the latest assistant turn with its tool results stay as they were. The summary goes into the user message after the original request word for word, followed by pending approvals and the act ledger, both verbatim. Taint flags are untouched. The summary is added to the thread as a `summary` item (trust `untrusted` if the run was tainted). Before/after token estimates go to the log and the audit table (`condense`). At most 2 condenses per run; if the prompt still exceeds the window the run stops with a clear message plus the summary so far (no provider escalation).
  - Still open: escalation approval card, thresholds measured per model, optional condensing on the Anthropic path (only the caps apply there).
