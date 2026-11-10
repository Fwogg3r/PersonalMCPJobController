---
title: "Run grants: one approval for a batch of calls (rules, templates, run state, standing grants)"
status: planned
links: [phase8/l, phase8/c]
---
pmjc_grant_request(intent, rules, minutes, max_calls): one approval covers matching calls. Closed rules (eq/in/range) vs open (re/any); command tools use templates with typed slots, no regex on shell strings. Run state clean/derived/untrusted; open grants cover clean runs only. Grant check only inside the act block (egress, writes_state and act-ledger guards unchanged). Standing grants in config, job-spec rules, revocation in dashboard, Discord and CLI. Design and review: DECISIONS.md "Run grants and permission levels", docs/reviews/2026-11-10-run-grants.md.
