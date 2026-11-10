---
title: "Visibility of local runs: streamed answer and thinking, prompt-reading progress, per-call log (pmjc completions)"
status: done
links: [phase6/k]
---
Notes: incident 2026-11-09: qwen3.8-27b loaded with parallel 4 + 65k context spilled out of the 24 GB GPU and read a 16k-token prompt at 15 tok/s (10+ min, silent chat). Reloaded with parallel 1: 12k tokens in 15 s (~830 tok/s). New provider options parallel (lms load --parallel) and stream; front_door.stream_answer and reasoning (field|think_tags|off); progress lines while the prompt is read (estimate from past speed, or LM Studio's log with extra.progress_log); completions table + pmjc completions. The owner's GPU-host provider switched to schema_mode compact + tool_mode select: tool payload ~14.2k -> ~3.6k tokens. Smoke test on that host: 2-turn pmjc-local run streamed 893 chunks with reasoning; calls logged. Review: private/reviews/2026-11-09-local-visibility.md. Tests: test_completions.py (14). Follow-up: Discord status-message edits.
