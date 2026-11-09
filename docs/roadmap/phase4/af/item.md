---
title: "Configure pmjc from chat: settings tools (safe-direction allowlist), profiles"
status: active
links: [phase4/x]
---
Decided 2026-11-09 after an Opus review (DECISIONS.md, "Configuring pmjc from chat"): settings_get with allowlisted paths only; settings_set refused when tainted or from jobs and watches, safe direction only with bounds, at most 3 paths of one risk class, approval with an escaped old->new diff and a two-step confirm for routing and cost; compare-and-swap under a file lock, patch only the changed keys, backups, revert as a diff; owner-authored profiles switched from chat.
