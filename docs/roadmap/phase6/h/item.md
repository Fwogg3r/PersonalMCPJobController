---
title: "Task runner: break a request into a plan and run it step by step with fresh context per step"
status: active
links: [phase6/e, phase8/c]
---
**What:** Higher-level requests ("plan my week from my calendar and email", "find and compare X") are broken into a short plan stored on the thread. Each step runs as its own agent run with fresh context: the goal, the plan with statuses, and short summaries of finished steps, instead of the whole transcript. Approvals pause the task and it resumes after the decision. Long single steps condense (phase6/e) as a backup.

**Why:** a 27B local model at 32k-64k can't hold a long tool transcript, and step boundaries are the natural place to drop context.

Design debate before building (taint across steps is the hard part).

**Decision (2026-11-09, after an Opus design review):** build the smaller pieces first: laundering fixes, output defanging, a derived sensitive flag, a quarantined `pmjc_research` sub-run, and bounded results with condensing (phase6/e). The general runner comes later, with a frozen plan. See DECISIONS.md, "Multi-step work on local models".

**Progress 2026-11-09 (research sub-run built):** `pmjc_research(question, max_sources)` runs a quarantined sub-run (src/pmjc/research.py): web tools only (config `research.tools`, private-output tools are dropped), a fixed research prompt with no model.md, history or memory tools, its own provider choice (local-only when the parent thread is private), and a capped untrusted summary plus the URLs it really fetched (config `research.max_chars`, `research.max_turns`). The executor allows egress after untrusted content only for a `Caller.quarantine` run that is not `sensitive`; only `research.new_caller` sets it. Tests: tests/test_research.py. Still to do: the general task runner (frozen plan, per-step tool sets), if real multi-step needs remain.
