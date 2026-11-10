---
title: MCP tools/list_changed when credentials or connectors change
nickname: Tool refresh
status: planned
links: [phase4/b]
---
**What:** When credentials or connector settings change (`pmjc auth set`, dashboard Connectors tab, settings reload), the MCP server sends `notifications/tools/list_changed` so connected clients pick up the new tools without a restart.

**Why:** On 2026-11-10, after `pmjc auth set ebay`, `pmjc_connectors` showed eBay as configured but the connected Claude Code session never got `ebay_search`; the test had to go through `pmjc call` instead.

**How:** Hook the registry reload (credential store write, settings save) to the MCP session(s) and emit the notification; test with a fake client that the tool appears after `auth set`.
