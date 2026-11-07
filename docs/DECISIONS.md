# Decisions

Design decisions and the reasoning behind them, newest first. Details of how each works are in [ARCHITECTURE.md](ARCHITECTURE.md).

## Generic connectors for everyday tools; automating repeated requests
- **`kind: command` YAML connectors** turn any command-line tool into pmjc tools without Python.
  - They run as argv, never through a shell.
  - Every parameter is checked against a `pattern` or `choices`, and values starting with `-` are refused.
  - The files are read from disk only, never created or edited through the API.
- **Built-in `files`, `pc`, `ssh` and `lmstudio` connectors** cover most "my own machines" needs.
  - **SSH** describes any machine by address, user, port, key, jump host or proxy command, or reuses a `~/.ssh/config` alias. It never sends passwords, and refuses values that SSH would read as options.
- **Automation is offered, never assumed.** pmjc remembers requests, groups repeated ones, and turns one into a job with a click. Such jobs notify only when there's something new. Auto-approval is an explicit per-tool list and never applies to runs that have read third-party content.
- **Home Assistant service calls need an explicit target** and honour `blocked_entities` and `blocked_patterns`, so devices that must never switch off (like a server's power socket) stay protected.

## Remote access: in-app auth instead of a login proxy
- **The obvious setup breaks claude.ai.** Putting a browser-login proxy in front of `/mcp` (e.g. a Cloudflare Access app) stops claude.ai and the mobile apps from connecting, because they can't complete a browser login.
- **So pmjc authenticates by itself:**
  - OAuth 2.1 through FastMCP with Google or GitHub, limited to an explicit identity allowlist (verified email required), for Claude's connectors;
  - scoped bearer tokens for Shortcuts, scripts and other CLIs.

  The server binds to localhost, and any tunnel can publish it.
- **Considered:** Cloudflare's managed MCP OAuth / MCP portals, which tie the setup to one provider, and a custom OAuth Worker, which is more code to own. Either can still sit in front, but neither is required.

## Watches: temporary monitors for one request
- **The request flow:**
  1. check what's connected;
  2. answer now if possible;
  3. otherwise start one watch (time, event or poll);
  4. it fires once, answers, delivers, and ends.
- **Watches are separate from jobs.** Jobs are recurring and owner-defined. Watches run even with background monitoring and scheduled jobs off, because the request itself authorizes them, and they turn off any source they started.
- **The model is the orchestrator,** guided by tool descriptions and server instructions. pmjc has no rule-based planner.
- **One process owns the scheduler.** Others (stdio MCP, CLI) write watches to the database, and the service adopts them every 5 s.

## Nothing in the background by default
- Background monitoring (per source), scheduled jobs and local-model auto-start all start **off**. With auto-start off, a local host counts only if the model is already loaded, so on-demand loading can't fill a GPU unasked.
- **The data folder on Windows is `%USERPROFILE%\.pmjc`, not AppData.** MSIX-packaged apps (such as the Claude desktop app) redirect the AppData writes of processes they start into a private copy, so a CLI started from them and a service started by Task Scheduler would otherwise see different folders.

## Security model (from an external review)
- **Fail-closed tool tags:** `@tool` defaults to `act`, and tools declare `untrusted_output` and `egress`.
- **Taint is derived:** after a run reads third-party content, egress tools are blocked and every action needs approval.
- **Approval on every agent interface,** stdio included. Approvals are tied to a payload hash, single use, and expire. Opening a link only shows the request; approving takes a POST.
- **`claude -p` is locked down:** its own config folder, no built-in tools, only pmjc over MCP, and no API keys in its environment.
- **Requests are checked:** Host allowlist and Origin check; nothing is trusted because of the source IP.
- **Secrets are encrypted** with an envelope key held in the OS keyring and redacted from logs.
- **Third-party plugin packages** load only when the user enables them.

## PC-native sensing
- **Notifications are read from the PC's notification center** through the WinRT `UserNotificationListener`, which works from unpackaged Python on Windows 11. The rejected alternatives:
  - a separate always-on phone;
  - Phone Link mirroring, which uses Bluetooth and stops when the phone leaves;
  - iOS, which has no API for reading other apps' notifications.
- Banners can be hidden per app, so only pmjc sees them.

## Modular core
- **One registry feeds every interface:** MCP, REST/OpenAPI, CLI, dashboard and tool-schema exports.
- **A connector is one Python class or one YAML file.**
- **Personal data never enters the repo:** settings, credentials, jobs and browser profiles live in `PMJC_HOME`.
- **Model routing is an ordered, user-configurable list:** local first, then Claude Code on the user's plan, then paid APIs with budgets. A run stays on one provider, and `private` jobs stay local.

## Cross-session context
- **Problem:** sessions are stateless and come from different vendors, but a request (a watch that fires later, a message waiting for a reply) outlives them.
- **Decision:** pmjc keeps the shared state: threads, notes, contacts and a user-editable `model.md`. Context reaches a model four ways, because no single one is reliable across clients: server instructions, a header on the first tool result of each MCP session, the `pmjc_context` tool, and the system prompt of pmjc's own runs. The first two work even if the model never calls `pmjc_context`.
- **Trust tiers instead of "tainted can't write".** Blocking writes from tainted sessions isn't enough: a clean agent can still be fed a poisoned value. So every item carries owner/agent/untrusted provenance; models are given owner text verbatim, agent text labelled and untrusted text never (only metadata); agents can't overwrite owner notes; agent-proposed contacts need the user's approval (a contact handle ends up in a deep link).
- **Act ledger.** The failure that matters when a session resumes is repeating a side effect. Recording tool + payload hash per thread, and asking for approval again on a repeat, is cheap and independent of the model's judgment.
- **Handoff over delegation.** When pmjc can't act as the user, it notifies them with a one-tap link rather than driving a more capable local agent. Delegation (`pmjc_delegate`) widens the attack surface (a second agent with browser control) and is deferred until it has its own threat model.
- **Limits live next to the tools.** `limitations` and `native_alternatives` are appended to each tool's description, so a blank session sees them in the tool list; `pmjc_capabilities` adds the task-level routes and drops the native and workaround routes for tainted sessions, so third-party text can't steer a model toward them.
- **Retention.** Closed threads are deleted after 90 days (`thread_retention_days`); `private` on a thread is sticky and keeps its asks on local models.

## Discord front door
- **Problem:** reach pmjc from any device without a tunnel, OAuth or a custom app, with push replies when watches and jobs finish.
- **Decision:** the service connects to the Discord gateway with the user's own bot and routes the owner's messages through `ask()`. Considered instead: a long-running Claude Code session with a Discord channel plugin (more capable, but it uses the plan for every message, stalls on permission prompts when unattended, and bypasses pmjc's scopes and approvals), and only the claude.ai connector (no push).
- **A small gateway client on `websockets` instead of discord.py:** the door needs identify, heartbeat, resume, messages and button clicks, about 100 lines with no new dependency. Revisit if it needs voice or sharding.
- **The owner's Discord id is the identity.** Messages from anyone else are never read. Runs are ordinary remote callers, so act tools still need approval; the approval is a button click checked against the owner id in the interaction, which the model can't produce.
- **Continuity:** `ask()` takes `history_turns`, so the last exchanges of the pmjc thread go back into the prompt. Third-party items in that history taint the run, as reading them through a tool would.
- **Slash commands as guild commands; `/tool` bypasses the model but not approvals.** Registered per server on connect (instant, unlike global commands), owner id checked on every interaction. `/tool` calls `executor.call_tool` as the door caller, so scopes, taint rules and Approve/Deny buttons apply unchanged. The per-call trace uses an `on_tool` hook on `Caller` that sees only name, args and status.
