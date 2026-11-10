---
title: "Local start fixes: wait for the start with progress lines, load the model on a running LM Studio, private work only on the local tier"
status: done
links: [phase6/j]
---
Notes: a running LM Studio server with nothing loaded was reported ready (its /v1/models lists every downloaded model), so the model was never loaded and pmjc-local failed with a misleading 'didn't start in 180 s'. healthy() now checks what is loaded and runs lms load. Interactive waits follow the shared start task (its own limits: start_timeout, >=300 s load), capped by local_start_wait_seconds (now 600), with a progress notice every local_start_notice_seconds (15). A start that ends unready fails at once and says so. Private work only uses the local tier (router.tiers.local; default all local providers). Failed start/load commands log their stderr. Tests: test_local_wait.py (+4); full suite 504 passed.
