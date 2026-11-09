---
title: Problem-solving instructions and fallbacks (use the tools you have, then commands/files with approval)
status: active
links: []
---
Live finding: the owner gave a Discord user id and asked to add it to a contact card; the run said it couldn't. Root cause found: pmjc_contact_set has scope 'jobs' but ask() only offers read tools, act tools and the WATCH_TOOLS list, and WATCH_TOOLS has pmjc_contact_find but not pmjc_contact_set, so the model never had the tool (not a model-intelligence limit). Fix that first (add pmjc_contact_set/pmjc_memory_set style writers to ask's tool set, review the list). Then: general system-prompt guidance for problem solving (check pmjc_capabilities, try the closest tool, explain what failed), and a last-resort path that proposes a terminal command or file edit (e.g. editing the contact file) which always needs approval, never runs from a tainted run, and is logged. Decided (owner, 2026-11-07): the runner is ordinary system shell commands (cmd/PowerShell batch commands on Windows, sh elsewhere) on the pmjc host, each one shown in full and run only after the owner approves it.

Progress (2026-11-08): quick fix done. ask() now offers pmjc_contact_set, pmjc_memory_set and pmjc_thread_start (deletes, job_run/job_delete and handoff stay out); test test_ask_offers_writer_tools. Implemented by the local model (Qwen3.8-27B via aider) under review. Still to do: problem-solving guidance in ASK_SYSTEM and the approved shell-command fallback.

Progress (2026-11-08, later): problem-solving guidance added as ASK_SYSTEM rule 7 (check pmjc_capabilities, try the closest tool such as pmjc_contact_set/pmjc_memory_set, otherwise name the missing tool, what was tried and the closest workaround); test test_ask_system_has_problem_solving_rule. Still to do: the approved shell-command fallback.

Decided (owner, 2026-11-08) for the shell fallback: who may propose is a config option (public default: untainted runs the owner started; jobs and watches opt in); timeout and output cap are configurable and the model may request other values per command, shown in the approval. See DECISIONS.md. Not built yet.
