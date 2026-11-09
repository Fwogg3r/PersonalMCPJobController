---
title: "Public-baseline audit: move owner-specific notes out of the public repo"
status: planned
links: [phase4/j]
---
Brief audit (2026-11-08) against the public-baseline rule (CLAUDE.md, DECISIONS.md "Approval outcomes and the shell-command fallback"). Clean: config defaults are generic (127.0.0.1, empty URLs and channel ids, keyring), no tokens, ids, hosts or addresses in src or tests; Fwogg3r appears only as the repo/licence identity. Fixed: a personal share path in CLAUDE.md. To resolve: (1) roadmap Notes for phase4/i and phase4/j, and others, name the owner's tooling (local model via aider, private/local-agent, the AgentController roadmap); reword them generically or move them to private/. (2) DECISIONS.md "Local model context size" is measured on the owner's own GPU and model; keep the method and move the specific numbers to a clearly labelled example, or to the private repo. (3) Check new features keep owner settings out of the code (the shell fallback, the deal-watch job in phase4/u).
