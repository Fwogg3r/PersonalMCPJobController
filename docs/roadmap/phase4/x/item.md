---
title: "Phone front door: OpenAI-compatible chat endpoint so any phone chat app talks to the pmjc assistant"
status: planned
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
