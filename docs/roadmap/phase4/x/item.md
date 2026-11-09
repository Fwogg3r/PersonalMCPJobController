---
title: "Phone front door: OpenAI-compatible chat endpoint so any phone chat app talks to the pmjc assistant"
status: done
links: [phase4/k]
---
**What:** One app on the phone for everything: pmjc serves an OpenAI-compatible `/v1/chat/completions` (and `/v1/models`) endpoint. The "model" is the pmjc assistant itself (ask() with routing, tools, threads and approvals), so any OpenAI-compatible chat app (mobile clients, Open WebUI, editor plugins) can talk to it over the existing remote access.

**How (proposal):**
- Bearer auth with the existing remote tokens; the caller counts as the owner (any authenticated route is the owner).
- Model ids select a mode: `pmjc` (router default), `pmjc-local` (private, local models only), plus any provider id.
- The app's conversation maps to a pmjc thread (a stable conversation id from the client where available, else a hash of the first message), so history and context carry over.
- Streaming (SSE) of progress and answer text, sharing the streaming work in phase4/k.
- Approvals: the reply says what's pending, with the approve link; approving in the app continues the thread (approval follow-ups already exist).
- Owner-specific setup (which app, the tunnel hostname) goes in the private repo, not here.

**Progress notes:**
- Built (`src/pmjc/openai_door.py`, `front_door` settings, tests in `tests/test_openai_door.py`, docs in `docs/FRONT_DOOR.md`): `GET /v1/models` (`pmjc`, `pmjc-local`, one `pmjc:<provider id>` per enabled provider) and `POST /v1/chat/completions` (JSON and SSE).
- Thread mapping: `X-Conversation-Id` header, else `conversation_id`/`metadata`/`user` in the body, else a hash of the first user message; always scoped to the token. Stored in table `door_threads`; only the last user message is sent, history comes from the thread.
- Streaming is a role chunk, optional `_calling tool..._` progress lines (`front_door.stream_progress`, default on), then the whole answer once ready, with a keep-alive comment every 10 s. True token streaming stays with phase4/k.
- Pending approvals for the run are appended to the answer with signed approve/deny links (prefixed by `public_url` when set). Usage fields are zeros until `ask()` reports token counts.
- Limits: body capped at 1 MB, 30 requests a minute per token (config).
