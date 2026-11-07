# PersonalMCPJobController (pmjc)

Read [AGENTS.md](AGENTS.md) first: setup, how agents use pmjc, and the rules for changing code. Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); decisions: [docs/DECISIONS.md](docs/DECISIONS.md).

- Public repo: nothing personal (names, hosts, addresses, tokens, settings, jobs, chat ids) is committed. Real data lives in `PMJC_HOME`; maintainer-only notes go in the gitignored `private/` folder.
- Tests: `uv run pytest`. Smoke test: `PMJC_HOME=<temp dir> uv run pmjc serve --port 8799`, never against the owner's real data folder.
- Don't leave test models loaded on a GPU someone else is using; unload them afterwards (`lms unload --all`).

<!-- roadmap-rule:start (managed by `roadmap agent-rule`) -->
## Roadmap (keep it current)

- `docs/roadmap/` is this project's roadmap and its single source of truth for planned, active and finished work (WebRoadmap format; view it with `roadmap serve`).
- **Every change must update the roadmap in the same commit:** set the entry's status when work starts or finishes (`roadmap set ID status=active|done`), add outcomes and numbers to its **Notes**, add entries for new work, ideas or findings (`roadmap add PHASE --title ...`), and fix links (`roadmap link A B`). Run `roadmap check` before committing.
- This applies to agents and subagents too: a brief or task that changes the project names the roadmap entry it updates.
<!-- roadmap-rule:end -->
