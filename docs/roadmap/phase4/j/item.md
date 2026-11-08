---
title: Problem-solving instructions and fallbacks (use the tools you have, then commands/files with approval)
status: planned
links: []
---
Live finding: the owner gave a Discord user id and asked to add it to a contact card; the run said it couldn't. Root cause found: pmjc_contact_set has scope 'jobs' but ask() only offers read tools, act tools and the WATCH_TOOLS list, and WATCH_TOOLS has pmjc_contact_find but not pmjc_contact_set, so the model never had the tool (not a model-intelligence limit). Fix that first (add pmjc_contact_set/pmjc_memory_set style writers to ask's tool set, review the list). Then: general system-prompt guidance for problem solving (check pmjc_capabilities, try the closest tool, explain what failed), and a last-resort path that proposes a terminal command or file edit (e.g. editing the contact file) which always needs approval, never runs from a tainted run, and is logged. Decided (owner, 2026-11-07): the runner is ordinary system shell commands (cmd/PowerShell batch commands on Windows, sh elsewhere) on the pmjc host, each one shown in full and run only after the owner approves it.
