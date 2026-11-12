---
title: pmjc_capabilities lists the connected tools that match the task (owner plugins and MCP mounts included)
status: done
links: [phase6/m, phase6/o]
---
**Why:** the capability catalog only knows shipped connectors. Asked 'which lights are on', the model called pmjc_capabilities, read 'Home Assistant isn't configured' and gave up, though the owner's command plugin had a live home_status. Same for API spend and roadmap tools.

**What:** tool_select.live_matches(task) scores configured tools by shared words (a hit in the tool name counts double; generic words such as check, status, off only break ties; stemmed like the rest of selection). pmjc_capabilities puts 'Connected tools that match: ...' first and the loader loads their connectors. Tests in tests/test_tool_select.py.
