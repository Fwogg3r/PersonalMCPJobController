---
title: Personal-data check before commit and push (private denylist hook)
nickname: Leak check
status: planned
links: []
---
**What:** A git pre-commit / pre-push hook that refuses a commit or push whose added lines contain terms from a gitignored denylist in `private/` (hostnames, personal names, ids, domains), plus common token patterns.

**Why:** The repo is public and must hold nothing personal. On 2026-11-10 a roadmap note naming the owner's GPU host was caught by hand just before a push and folded out of the unpushed history.

**How:** A small `uv run` script installed as a hook by a `pmjc dev hooks` command or documented one-liner; the denylist file never leaves `private/`; the script fails with the file and line, never prints the matched secret-looking value in full.
