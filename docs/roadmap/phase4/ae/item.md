---
title: "Snappy commands and approvals: run ids for long commands, instant Discord button acks"
status: done
links: [phase4/ad, phase4/i]
---
Owner request 2026-11-09 ("as snappy as possible with the model"). Two live bugs.

**Discord Approve sometimes needed a second click.** The first click was acknowledged (type 6 deferred update, which shows nothing), but the message was only edited after `decide()` had run the tool, so a slow tool left a message that looked untouched; the owner clicked again, got "already decided" patched over the message, and the real outcome edit raced it. Fix: type 7 update at once (buttons removed, "Approved by ... running"), `decide` in a background task, outcome edit via the webhook with a fallback to a channel message edit when the 15-minute token is gone, and a double-click guard (in-process claim before the first await plus the approval status): a repeat click gets a private "already running / already done". Deny uses the same path. Tests: test_discord_door (ack before the tool finishes, edit with outcome, double click runs once, deny, token fallback).

**Commands blocked the model until every process holding their pipes exited** (`start cmd /k` inherited the pipes), and long commands blocked for their whole runtime. Fix (`commands.py`): Popen plus reader threads, wait for the direct process, 1 s grace for output readers, then return with `detached_children` and close our pipe ends; `wait_seconds` (config `commands.wait_seconds`, default 8) after which a command returns `{status: running, run_id}` and keeps running under its timeout; in-process registry plus `command_runs` table; new tools `command_status(run_id, wait_seconds)` (read, untrusted output, owner or admin only) and `command_stop(run_id)` (act, approval). Used by `ssh_run`, `ssh_check` and `pmjc_shell_run`. An approved slow `ssh_run` now finishes `decide()` within about `wait_seconds`. Tests: tests/test_commands.py.

Notes: live output exists only in the process that runs the command; other processes see status and the last 4000 characters. A wait_seconds argument is not part of the stored `pmjc_shell_run` approval (the config default applies after approval). The roadmap id ae was the next free one after ad (ab and ac are used on other branches).

Follow-up (2026-11-09, live check on the pmjc host): commands with double quotes failed on the self route and in pmjc_shell_run with cmd, because list quoting turned inner quotes into \" ; cmd now gets the command line verbatim (`cmd /d /s /c "<command>"`). Verified: a detached child returns in ~1 s with detached_children, a 15 s command returns running with a run id after 8 s, quoted paths work.
