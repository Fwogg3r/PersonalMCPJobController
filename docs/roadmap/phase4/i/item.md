---
title: "Act on outcomes: approval results and errors return to the run, which retries, fixes or asks"
status: planned
links: [phase4/j]
---
Today a run that hits an act tool gets pending_approval and ends; the decision (approved/denied/failed) only lands in the thread log, so the model never sees it. Wanted: approval outcomes and tool errors flow back into the same conversation and the model continues: on success it confirms, on error it tries a fix (different args, another tool) up to a small budget, then asks the user with the error text. Design: after executor.decide, resume the pmjc thread with an 'approval result' turn (ask() with history) instead of only logging; for errors inside a run, feed the error back as the tool result (already done) and add a retry/ask-for-help rule to ASK_SYSTEM. Keep the act ledger and the dual-LLM rule (a tainted run still can't act). Discord: post the outcome under the approval message and continue the thread.
