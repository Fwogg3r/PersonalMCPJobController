---
title: "SSH self route: the pmjc host is reachable as a machine, so any device can run commands on it through pmjc"
status: done
links: []
---
Found 2026-11-09: a Claude session on another device asked pmjc to run a command on the pmjc host and got "no ssh route", because ssh_run only knew other machines and an SSH alias for the host itself fails. Done: the Ssh connector always lists the pmjc host (its hostname and `local`; configured names matching the host or in `self_names` map to it) and runs those commands locally with `self_shell` (cmd on Windows to match Windows OpenSSH, sh elsewhere); same approval as any ssh_run. Tests: test_ssh_self_route, test_ssh_run_on_self_still_needs_approval.
