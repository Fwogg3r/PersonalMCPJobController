---
title: Mount existing MCP servers as connectors; OpenAPI import
status: active
links: []
---

Progress (2026-11-09): part 1 done, mounting existing MCP servers. `mcp_servers:` in settings.yaml (stdio argv or streamable HTTP, env and bearer token by credential-store name, `tools`/`exclude`, per-tool `overrides` of scope/untrusted_output/egress) is turned into one connector per server by `pmjc.mcp_connector` (FastMCP client on a shared background loop, connections pooled across registry reloads, reconnect once on failure, health shows connected/failed and tool count). Tools are named `<id>__<tool>` (sanitized, capped at 64), the server's input schema is passed through (full and compact schema modes work), calls go through the executor. Defaults: scope `act` unless `readOnlyHint: true`, `untrusted_output` and `egress` true; recorded in DECISIONS.md. `pmjc mcp list` shows servers, tools and effective flags (bare `pmjc mcp` still runs the stdio server). Tests in tests/test_mcp_mount.py with a stdio fake server. Still to do: OpenAPI import (not started); image/binary results are only noted as omitted; per-server tool refresh without a registry reload.

Progress (2026-11-10): server-wide `egress` default on `mcp_servers` entries (`egress: false` for a mount that only touches the owner's storage, e.g. a notes vault; a per-tool override still wins; unset stays fail-closed). Lets a tainted run propose vault writes, which still need approval. 2 new tests in tests/test_mcp_mount.py (full suite: 511 passed, 2 skipped). DECISIONS.md and CONNECTORS.md updated.
