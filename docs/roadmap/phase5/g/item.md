---
title: "Configure pmjc from chat: settings tools (safe-direction allowlist), profiles"
status: done
links: [phase4/x]
---
Decided 2026-11-09 after an Opus review (DECISIONS.md, "Configuring pmjc from chat"): settings_get with allowlisted paths only; settings_set refused when tainted or from jobs and watches, safe direction only with bounds, at most 3 paths of one risk class, approval with an escaped old->new diff and a two-step confirm for routing and cost; compare-and-swap under a file lock, patch only the changed keys, backups, revert as a diff; owner-authored profiles switched from chat.

## Notes
- Implemented (2026-11-09): `settings_edit.py` (path registry with type, bounds, direction and risk class; hard-deny set checked first), tools `pmjc_settings_get`, `pmjc_settings_set`, `pmjc_settings_revert`, `pmjc_profile_switch`, and `Settings.profiles`.
- Gates: refused outright (no approval row) for tainted runs, jobs, watches and approval follow-ups; never auto-approved; 3 proposals per hour per caller and a 30-minute cooldown per thread after a denial (both derived from the approvals table).
- Apply: `config.file_lock` (O_EXCL lockfile, taken by `config.save` too), compare-and-swap on the sha256 stored in the approved args, ruamel patch of only the changed keys, full model validation plus invariants, backup to `<data>/settings-backups/` (last 20, unreadable through the files connector), atomic replace, audit rows. `config.load()` has no cache, so no version bump was needed.
- Approval view shared by Discord (Review button, then Approve), the link page (review page, then form) and the dashboard (second click).
- Open: the Approve button is not delayed by about 5 seconds after Review (Discord cannot enable a button by itself); the Review step is the friction. Tiers and route rules stay desktop-only until a trust invariant check covers them.
- Owner chat commands (no model, work with every model off): `/settings`, `/set`, `/profile`, `/revert` in `chat_commands.py` via `settings_edit.owner_command`. Needs the `actions` scope. Off-lower `/set` applies at once (audit says owner command); routing and cost `/set`, profile switches and reverts reply with an approval diff and signed links. Not added to the Discord door's slash commands.
