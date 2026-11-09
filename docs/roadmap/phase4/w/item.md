---
title: "Per-provider tool selection: small-context models get a core set plus tools loaded on demand"
status: active
links: [phase4/p]
---
**Why:** every ask() run sends every read, watch and act tool schema to the model on every turn. Measured 2026-11-09 with the default connectors: 37 tools, about 28k characters (~7k tokens), more with optional connectors enabled. On a 32k-context local model that is over a fifth of the window before the prompt, model.md or any tool result. Jobs and watches can already narrow this with their `tools` list; ad-hoc asks (Discord, REST, CLI) can't.

**Original proposal (superseded, see Decision below):**
- Provider option `tool_mode: all | deferred` (default `all`; set `deferred` on small-context providers). Claude and big-context providers keep today's behaviour.
- Deferred mode sends a small core set (pmjc_capabilities, pmjc_connectors, thread/memory/contact tools, watch tools) plus a one-line catalog of the other connectors in the system prompt, and a `pmjc_tools_load(connectors)` tool that adds those schemas from the next turn on.
- Safety net: if the model calls a tool it hasn't loaded, load it and reply "loaded, call it again" rather than failing.
- Cheap pre-load: connectors whose names or aliases appear in the request (or the thread's earlier turns) are loaded up front, so most runs need no extra turn.
- Loading a schema never changes permissions: scopes, taint and approvals are checked at call time as today.

Links: complements auto-condense (phase4/p), which handles growth inside a run; this cuts the fixed overhead. Design debate before building.

**Decision (2026-11-09, after an Opus design review):** compact schemas first (provider option `schema_mode: compact`), measure, then a fixed per-run toolset chosen before turn 1, with `pmjc_capabilities(task)` as the loader and an executing safety net. Full reasoning in docs/DECISIONS.md, "Tool selection for small-context models".
