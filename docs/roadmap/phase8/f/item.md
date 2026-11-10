---
title: "CI: pytest and roadmap check on every push"
nickname: CI
status: planned
links: []
---
**What:** A GitHub Actions workflow that runs `uv run pytest` and `roadmap check` on every push and pull request.

**Why:** There is no CI today; the suite takes about 5 minutes and is only run locally, so a push of many commits (63 on 2026-11-10) can carry breakage unnoticed.

**How:** `astral-sh/setup-uv`, Windows and Linux runners (pmjc targets Windows first), no secrets needed because tests mock every service. Pin action versions.
