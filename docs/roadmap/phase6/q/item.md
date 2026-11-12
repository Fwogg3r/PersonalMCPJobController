---
title: Reload a local model loaded with too small a context (JIT 8k) and retry once on exceed_context_size_error
status: done
links: [phase6/k]
---
If another client (or LM Studio's JIT load) loaded the local model first, it runs at LM Studio's small default context (8192 tokens), and pmjc used it as is: on 2026-11-11 a Discord-door run sent a 16231-token prompt to an 8192-token context and failed with `exceed_context_size_error`.

## Notes

- `router.ensure_context(p)`: reads the loaded instance's context (`/api/v0/models` `loaded_context_length`, else `lms ps --json` `contextLength`); when it is below the provider's `context_tokens`, runs `lms unload <model>` then the usual load command (`--context-length context_tokens`).
- Called from `healthy(start=True)` before every local run. `healthy(start=False)` (interactive requests) reports a short context as not ready when a reload is possible, so the background start reloads it.
- Guards: needs `auto_start_local` and `load_model`, a local host and the `lms` CLI; skipped while the GPU guard says busy or pmjc has a local request in flight; waits up to 60 s for another client's generation (`lms ps` status) and logs a warning if it unloads a model still in use. Otherwise the model is used as is with a warning.
- A run that gets `exceed_context_size_error` (or "greater than the context length") calls `ensure_context` and retries the turn once, only if it actually reloaded; a prompt that is too big for `context_tokens` itself still fails over to the next provider.
- Tests: `tests/test_context_reload.py` (14, fake LM Studio REST through an httpx MockTransport, fake `lms`). Not verified against a real LM Studio.
