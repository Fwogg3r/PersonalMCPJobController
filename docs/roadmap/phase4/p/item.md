---
title: "Auto-condense: keep local-model runs going near the context limit, escalate with approval"
status: planned
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
