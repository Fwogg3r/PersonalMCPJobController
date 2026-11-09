---
title: "Public-baseline audit: move owner-specific notes out of the public repo"
status: done
links: [phase4/j]
---
Brief audit (2026-11-08) against the public-baseline rule (CLAUDE.md, DECISIONS.md "Approval outcomes and the shell-command fallback"). Clean: config defaults are generic (127.0.0.1, empty URLs and channel ids, keyring), no tokens, ids, hosts or addresses in src or tests; Fwogg3r appears only as the repo/licence identity. Fixed: a personal share path in CLAUDE.md. To resolve: (1) roadmap Notes for phase4/i and phase4/j, and others, name the owner's tooling (local model via aider, private/local-agent, the AgentController roadmap); reword them generically or move them to private/. (2) DECISIONS.md "Local model context size" is measured on the owner's own GPU and model; keep the method and move the specific numbers to a clearly labelled example, or to the private repo. (3) Check new features keep owner settings out of the code (the shell fallback, the deal-watch job in phase4/u).

Done (2026-11-09): (1) phase4/i and phase4/j notes now describe "a local-model coding agent" without naming the owner's tooling; (2) the context-size decision keeps its measurements, labelled as one example setup to re-measure on your own hardware; (3) the deal-watch entry (phase4/u) no longer lists the owner's targets and thresholds (they belong in the user's own job file); the new shell fallback and declared-read options ship with generic defaults only. Originals kept in the gitignored private/ folder. A grep of docs, src and tests for owner tooling, hosts and hardware names is clean apart from the labelled example.

README refreshed (2026-11-09) for waves 1-3: front door, routing, small-model tool handling, web/eBay/Craigslist, MCP mounting, instances, taint hardening, approval wait, snappy commands, evals.
