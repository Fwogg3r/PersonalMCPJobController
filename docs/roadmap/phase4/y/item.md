---
title: "Task runner: break a request into a plan and run it step by step with fresh context per step"
status: active
links: [phase4/p]
---
**What:** Higher-level requests ("plan my week from my calendar and email", "find and compare X") are broken into a short plan stored on the thread. Each step runs as its own agent run with fresh context: the goal, the plan with statuses, and short summaries of finished steps, instead of the whole transcript. Approvals pause the task and it resumes after the decision. Long single steps condense (phase4/p) as a backup.

**Why:** a 27B local model at 32k-64k can't hold a long tool transcript, and step boundaries are the natural place to drop context.

Design debate before building (taint across steps is the hard part).

**Decision (2026-11-09, after an Opus design review):** build the smaller pieces first: laundering fixes, output defanging, a derived sensitive flag, a quarantined `pmjc_research` sub-run, and bounded results with condensing (phase4/p). The general runner comes later, with a frozen plan. See DECISIONS.md, "Multi-step work on local models".
