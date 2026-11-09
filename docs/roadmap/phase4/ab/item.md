---
title: "Taint hardening: approval for writers after untrusted content, output defanging, derived sensitive flag"
status: active
links: []
---
Steps 1-3 of the decision "Multi-step work on local models" (DECISIONS.md, 2026-11-09). Found in the Opus review: pmjc_memory_set, pmjc_contact_set, pmjc_job_create and pmjc_watch_start are jobs-scope (not act), so a run that read untrusted content could write attacker text into memory that lands in every later system prompt.
