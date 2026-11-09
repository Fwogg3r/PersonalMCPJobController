# Architecture

PersonalMCPJobController (`pmjc`) is a self-hosted **switchboard**. It connects agents and automation tools to the services a person uses, and runs scheduled or event-driven jobs over them. It runs as an always-on service on one PC, and anyone can clone it and run it on their own machine.

```
                 ┌──────────────────────── interfaces ────────────────────────┐
 Claude / Cursor │ MCP stdio · MCP HTTP (/mcp, token or OAuth)                │
 n8n / HA / Zap  │ REST + OpenAPI (/api, /docs) · webhooks in (/hooks) · CLI  │
 any LLM agent   │ OpenAI/Anthropic tool-schema export (/api/tools/export/..) │
 you             │ dashboard (/) : connectors, credentials, routing, jobs     │
                 └───────────────┬─────────────────────────────┬─────────────┘
                                 │ executor: scopes · taint · approvals · audit
      ┌──────────────┐   ┌───────▼────────┐   ┌──────────────┐   ┌────────────┐
      │ credential   │◄──┤ connector      │──►│ event bus    │──►│ job engine │
      │ store        │   │ registry       │   │ (SQLite)     │   │ (APSched.) │
      └──────────────┘   └───────┬────────┘   └──────────────┘   └─────┬──────┘
                                 │ tools / event sources / notify      │
   windows_notifications · browser · home_assistant · discord · telegram │
   slack · google · github · weather · places · ntfy · webhook · YAML APIs│
                                                       ┌─────────────────▼──────┐
                                                       │ model router + agent   │
                                                       │ local hosts → claude -p│
                                                       │ (plan) → paid APIs     │
                                                       └────────────────────────┘
```

Code map (`src/pmjc/`):

| File | What it holds |
|---|---|
| `connector.py` | The connector contract |
| `registry.py` | Discovery |
| `http_connector.py` | YAML connectors |
| `executor.py` | The one path for every tool call |
| `auth.py` | Tokens, callers, signatures |
| `creds.py` | Credential store |
| `router.py`, `agent.py` | Model routing and the tool loop |
| `jobs.py` | Jobs, events, scheduler |
| `mcp_server.py`, `server.py` | Interfaces |
| `service.py` | Always-on service |
| `cli.py` | CLI |
| `connectors/` | Built-in connectors |

## Principles
1. **PC-native first.** Sensors run on the host PC: Windows notifications from desktop apps and Chrome, plus a signed-in browser profile. No extra hardware.
2. **One connector = one small file.** A connector declares its credentials, its tools and optional event sources. A service with a plain HTTP API can be YAML only. Third-party packages can add connectors through the `pmjc.connectors` entry point, but only when the user enables them.
3. **Nothing personal in the repo.** Credentials, jobs, settings, browser profiles and the database live in `PMJC_HOME`:
   - Windows: `%USERPROFILE%\.pmjc` by default. Not AppData: MSIX-packaged apps, such as the Claude desktop app, redirect the AppData writes of processes they start into a private copy that the service would never see.
   - Elsewhere: `~/.local/share/pmjc`.
4. **Least privilege, fail closed.** There are four scopes:
   - `sources`: read;
   - `actions`: side effects, which need approval;
   - `jobs`;
   - `admin`.

   Tools count as side-effecting unless they say otherwise.
5. **Every interface is generated from the same registry, and every call goes through `executor.call_tool`.** Picking an interface can't bypass the rules.

## Connector contract
See [CONNECTORS.md](CONNECTORS.md).
- **Browser.** `browser_read` (text plus links) and `browser_extract` (rows from a CSS selector and field map) share one persistent profile and the `allowed_domains` check. Options: `connectors.browser.channel` (empty = bundled Chromium, `chrome` = installed Chrome) and `headless` (default true; headed windows open on the rightmost monitor without taking focus).
- **Tags.** `@tool(scope="read"|"act"|"jobs", untrusted_output=..., egress=...)`. The default is `act`. YAML tools default to act, untrusted output and egress.
- **Tool names** are `<connector>_<verb>`: letters, digits and underscores, at most 64 characters, checked when the connector loads.
- **Credential fields:** secrets go to the credential store; `secret=False` fields go to `settings.yaml`. There are `oauth2` (`OAuth2Mixin`: authorization code + PKCE, refresh under a lock) and browser-login flows.
- **Event sources** return `Event`s. The bus dedupes them by `key`. Connectors keep cursors with `state_get` and `state_set`.

## Credential store
- **Envelope encryption.** Secrets are encrypted with one random data key (Fernet) and stored in the pmjc database, so there are no keyring size limits and no per-item OS prompts.
- **Where the data key lives:** the OS keyring (Windows Credential Manager, macOS Keychain or Secret Service), or derived from `PMJC_MASTER_KEY` (`file` backend, for headless Linux; inject it with systemd-creds).
- **Env overrides:** `PMJC_<CONNECTOR>_<FIELD>` always wins and is read-only.
- **Redaction:** a logging filter removes every loaded secret from logs and exception text, and httpx request logging is off because URLs can carry keys. YAML connector credentials are only sent to their `base_url`, and path parameters are URL-encoded.
- **Threat model:** this protects secrets at rest and keeps them out of the repo. Any process running as the same OS user can still read them, as with any keyring or browser profile.

## Model router
- An ordered, configurable list of providers (dashboard → Routing). The default order:
  1. **Local OpenAI-compatible hosts** (LM Studio, Ollama, ...). Health means the wanted model is listed. If the host is down, pmjc runs its `start` command (e.g. `lms server start`, or `ssh gpu-box lms server start`); if the model isn't loaded, `lms load`. `skip_when_gpu_busy_percent` skips local hosts while the GPU is busy.
  2. **`claude -p`** on the user's plan (locked down; see below).
  3. **Paid APIs** (Anthropic, OpenAI, OpenRouter, any OpenAI-compatible URL), each with a monthly budget cap enforced from reported usage. Also set provider-side limits, since the price table can go stale.
- **Skipping.** A provider is skipped when it's disabled, unhealthy, over budget, cooling down after a failure (5 minutes), or lacks a needed capability (`tools`, `vision`, `context_tokens`). Jobs can override the order.
- **Tool schema size.** A provider with `schema_mode: compact` gets first-paragraph tool descriptions, no schema titles, flattened optional types, and each connector's limits once in the system prompt (default `full`). `pmjc tools size` shows the saving.
- **Privacy.** `trust: local|cloud` on each provider. A job with `private: true` uses local providers only and fails rather than fall back to the cloud.
- **Pinned per run.** If the provider fails, or returns invalid JSON when JSON was expected, before any act tool has run, the run restarts on the next provider. After an act tool has run, the run is marked failed, so side effects never repeat.
- **Start and load commands** run as argv with no shell and a timeout, and can only be changed in `settings.yaml`, never through the API.
- **`claude -p` lockdown:**
  - its own `CLAUDE_CONFIG_DIR`, and `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`;
  - an empty temp cwd, so no CLAUDE.md is picked up;
  - `--tools ""` (no built-in tools);
  - `--strict-mcp-config` with only pmjc, reached with an in-memory run token bound to the job's caller;
  - `--allowedTools mcp__pmjc`;
  - `ANTHROPIC_*` and Bedrock/Vertex variables removed from its environment.

## Jobs
- **Files.** A job is a YAML file in `PMJC_HOME/jobs/` (examples in `examples/jobs/`). The schedule is rebuilt from the files at start and whenever they change (APScheduler 3, coalesce, misfire grace period).
- **Triggers:** `cron`, `interval`, an `event` filter, a webhook (`/hooks/<name>` creates an event), or `manual`.
- **Tiers:** plain-code `match` filters (app, contains, regex, data) and dedupe first; the agent loop only if they pass.
- **Taint is derived, not declared.** A run is tainted once its trigger event is untrusted or it calls an `untrusted_output` tool. A tainted run can't call `egress` tools, and every act tool goes to approval, even ones on the auto-approve list. One exception: `browser_read`/`browser_extract` on a URL or site the owner declared in the job (`read_urls`, `read_sites`) or watch spec, since the owner fixed it (`executor.declared_read`; see DECISIONS).
- **No escalation.** A job can't give itself more than its creator has: scopes are limited to `sources` and `actions`, and `auto_approve` is honoured only in jobs the owner wrote (CLI, dashboard, the file). Jobs can't create jobs.
- **Output** goes to the job's notify channels. `NOTHING` or empty means no notification.

## Requests and watches
- **Request flow:** check connections, answer instantly when possible, otherwise start one **watch**. `/api/ask` and `pmjc ask` run this flow on the routed model; MCP clients run it with their own model. Both are guided by the tool descriptions and the server instructions.
- **A watch** is a temporary monitor for one request (`watches.py`, table `watches`). Types:
  - `time`: date trigger.
  - `event`: matches events from one source with the same filter as jobs.
  - `poll`: calls a *read* tool every N seconds (minimum 30) and fires on contains/regex/changed. An optional yes/no check goes to a local model (`private`).
- **Firing:** a watch fires once. The request is answered by the routed model with the gathered context, tainted for event and poll triggers, or sent raw if no model is available. The answer is delivered to the notify channels and stored. Then the watch ends, and `apply_settings` turns off sources nobody needs.
- **Polling is on while needed:** a source polls when global monitoring includes it **or** an active watch needs it. Events seen only for a watch are routed but not stored.
- **Limits and persistence:** watches expire (default 24 h, max 7 days) and need the `jobs` scope. They're stored in SQLite: the service adopts watches created by other processes (stdio MCP, CLI) every 5 s, and restores them after a restart.

## Approvals
- **When.** An `act` tool called through **any** agent interface (stdio MCP included) creates a pending approval, unless it's on the owner's `auto_approve` list and the run is clean. Only a person at the CLI or dashboard runs act tools directly.
- **What's stored:** the exact arguments plus a payload hash. It's single use and expires after about 10 minutes. On approval the stored payload runs; it is never regenerated.
- **MCP clients get the outcome (no re-prompting).** When an act call from an external MCP client (stdio, remote token or OAuth; never pmjc's own ask/job/watch runs, nor a `pmjc_run_` subprocess token) goes pending, `mcp_server` waits inline up to `approval_wait_seconds` (default 50, under the ~60 s tool-call timeout many clients use) and returns the real outcome as the tool result (`done` + result, `denied`, `failed`, `expired`). Still pending: the payload carries a `next` note telling the model to call `pmjc_approval_wait(approval_id, timeout_seconds=50)` (read scope, max 1800; only the requesting caller or an admin). The wait is `executor.wait_for_decision`: an in-process event set by `decide()`, plus a poll of the approvals table every `approval_poll_seconds` (default 2) because the stdio process and the HTTP server that handles the approval are different processes. The wait is cancelled if the client disconnects.
- **No duplicate follow-ups.** A waiter heartbeats `approvals.waiter_seen`; on returning the outcome it sets `delivered_to_requester=1`. `ask.continue_after_approval` then only writes a thread note (no pmjc model run, no notify message) when the outcome was delivered, or a live waiter exists (it gives the waiter a short grace period to deliver). If nobody waited, the thread continuation and notification run as before.
- **Announced** on the first working notify channel (HA actionable notification, Telegram button, ntfy action, Discord or Slack link, webhook) with signed links.
- **How to approve:** only through the dashboard (admin) or an HMAC-signed link. The link's GET only shows the arguments; approving takes a POST, so link previews can't approve. **No tool can approve.**

## Commands that don't block (`commands.py`)
- `ssh_run`, `ssh_check` (remote and the pmjc host's self route) and `pmjc_shell_run` share one runner: `subprocess.Popen` plus reader threads (`CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` on Windows). It waits for the **direct** process, then gives the output readers 1 s; pipes still open after that were inherited by something the command started (e.g. a window opened with `start`), so the result carries `detached_children: true` and our pipe ends are closed (from helper threads, since closing can block on Windows while a reader is mid-read).
- `wait_seconds` (argument; default config `commands.wait_seconds`, 8): a command still running then returns `{status: "running", run_id, output_so_far, next}` and keeps running in the background, still killed (with its children) at its timeout. `command_status(run_id, wait_seconds=20, max 50)` waits for completion or new output and returns the new output plus the exit code once done; `command_stop(run_id)` (act, approval) kills it.
- State: an in-process registry (live output, only in the process running the command) and the `command_runs` table (status, exit code, last 4000 characters), so another process, such as the stdio MCP process, sees how it ended. Only the starting caller (approvals: the requester) or an admin sees a run; strangers get `unknown`. `command_status` output is `untrusted_output`, like `ssh_run` and `pmjc_shell_run`.
- Approvals: an approved command that goes `running` makes `decide()` return within about `wait_seconds` with `{status: done, result: {status: running, run_id}}`; the inline wait and `pmjc_approval_wait` deliver that and the model follows up with `command_status`. The act ledger records the approval once.
- Discord buttons: Approve/Deny answer the click at once (type 7 update, buttons removed), decide in a background task and edit the message with the outcome (webhook, else channel message edit after the token expires). A repeat click gets a private "already running/done" and never runs the tool twice.

## Shell-command fallback
- **`pmjc_shell_run`** (`shell.py`) is the last resort when no tool can do a job: it proposes one ordinary system shell command (PowerShell on Windows, sh elsewhere) for the pmjc host. It's an `act` tool with `untrusted_output`, so the follow-up after it is tainted.
- **Always approved.** The executor forces the approval flow for it: never auto-approved, not even for a person at the CLI. The approval stores the full command plus the effective shell, working directory, timeout and output cap; commands over 700 characters are refused so every approval view shows all of it. Proposal, decision and result are audited.
- **Never from a tainted run.** A run that read third-party content can't propose it.
- **Who may propose:** config `shell_command_proposers` (default `[owner]`: Discord DM, REST, CLI and MCP clients; add `jobs` and `watches` to allow those; empty turns it off). Anything else, such as approval follow-ups, is refused.
- **Limits:** `shell_command_timeout_seconds` (30) and `shell_command_output_cap_chars` (10000) are the defaults; the model may request other values, clamped to `shell_command_max_timeout_seconds` (300) and `shell_command_max_output_chars` (100000). The shell (`shell_command_shell`, auto) and working directory (`shell_command_cwd`, default the home folder) come only from config.

## Interfaces and auth
- **MCP stdio:** `pmjc mcp`, for local clients. All scopes, but act tools still need approval.
- **MCP HTTP** at `/mcp` and the **REST API** at `/api/*`:
  - bearer tokens (`pmjc token create`, stored as SHA-256 hashes);
  - optional OAuth 2.1 (FastMCP Google or GitHub provider through `MultiAuth`). It refuses to start without `oauth.allowed` (identity → scopes).
- **Taint tracking** for HTTP MCP clients is per `Mcp-Session-Id`. REST calls are stateless, so taint is per request.
- **Network.** The server binds to `127.0.0.1` by default. Remote access goes through a tunnel the user runs. Nothing is trusted because of the source IP, since a tunnel makes every request come from localhost.
- **Request checks.** A Host allowlist and an Origin check run on every request, against DNS rebinding and cross-site requests.
- **Dashboard** at `/`: a bearer token held in sessionStorage (no cookies, so no CSRF) and a strict CSP. The kill switch revokes every token and pauses the scheduler.

## Service
`pmjc service install` registers the server to start at logon or boot and to restart if it crashes:
- **Windows:** a per-user scheduled task at logon. It must be the interactive session to read notifications and drive the browser.
  - Task settings: `pythonw.exe` (no console window), `ExecutionTimeLimit=PT0S`, battery stop/start off, `RestartOnFailure` every minute, `IgnoreNew`.
  - The supervisor holds a single-instance lock, keeps the PC awake, and restarts the server child with backoff.
  - After a reboot it starts when the user signs in. Enable Windows auto sign-in if the PC must recover with nobody there; that's a security trade-off.
- **Linux:** a systemd user unit; `loginctl enable-linger` keeps it running after logout.
- **macOS:** a launchd agent.

## Windows notifications
- **Listener.** `UserNotificationListener` (WinRT, pywinrt packages) works from plain unpackaged Python on Windows 11. This was verified on 2026-11-07, with access status Allowed. pmjc polls `GetNotificationsAsync` every 3 s and diffs by notification ID; it doesn't use the change event, which unpackaged callers don't get. It doesn't replay the notification center at startup.
- **Fallback.** A WAL-aware, read-only copy of `wpndatabase.db`. It fails loudly if the schema changes.
- **OTP redaction.** Codes in sign-in and verification notifications become `[code]` by default.
- **Silencing.** `pmjc notifications silence <AUMID>` sets `Enabled=1`, `ShowInActionCenter=1` and `ShowBanner=0` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\<AUMID>`, creating the key if it's missing. The value names for sound and lock screen aren't certain, so `pmjc notifications learn <AUMID>` records the exact values from a manual toggle.
- **Chrome** shows every site under one app ID. Install a site as an app, or give it a separate profile, to silence it alone.

## Context across sessions
`memory.py` owns the state (SQLite tables `threads`, `thread_items`, `thread_acts`, `notes`) and `model.md` in `PMJC_HOME`.
- **Trust tiers.** `trust_of(caller)` gives owner (admin caller), untrusted (tainted caller) or agent. Items, notes and threads store it. Anything shown to a model goes through functions that never emit untrusted text (`threads_recent`, `context_bundle`, `context_header`); the only way to read it is `pmjc_thread_get`, an `untrusted_output` tool, so the reader is tainted by the normal executor rule.
- **Writes.** Tainted callers can't write notes or contacts. Agents can't overwrite or delete owner notes. Agent-proposed contacts are stored under `pending:contact:<name>` and only an admin endpoint promotes them.
- **Threads and runs.** `Caller.thread_id` binds a run to a thread (set by `pmjc_thread_start`/`pmjc_thread_get`, by `ask`, by watches and jobs). Approvals and job runs record it, and approval outcomes append to the thread (tool name and status only).
- **Act ledger.** After an act tool runs in a thread, `thread_acts` records tool + payload hash. `executor.call_tool` asks for approval again, even for auto-approved tools, when the same pair repeats in that thread.
- **Delivery.** Watches create or reuse a thread, append the result (untrusted when it came from an event, poll or tainted run), mark it updated and unread, and add `Resume: thread ...` to the notification. `model.md` goes into the system prompt of ask, jobs and watches (`memory.system_block`, 8 KB cap, with a version hash), into the FastMCP instructions, and into a header prepended to the first tool result of each MCP session (`mcp_server.with_first_call_header`, keyed by `Mcp-Session-Id`, or per process on stdio).
- **Capability awareness.** `Connector.limitations` / `native_alternatives` (also YAML keys) are appended to tool descriptions by the registry. `capabilities.CATALOG` is the task-level view behind `pmjc_capabilities` and `docs/CAPABILITIES.md`; for tainted callers the native and workaround routes are dropped.
- **Handoff.** `handoff.py` validates contact handles and builds deep links; `pmjc_handoff` notifies, records and can start an event watch on `windows_notifications` linked to the thread. Refused for tainted callers.

## Deferred (see the roadmap)
- `pmjc_delegate`: running a user-configured, more capable local agent for tasks pmjc's tools can't do. Needs its own threat model (argv templates from settings only, act scope, never from tainted runs).
- Multiple accounts per connector (`gmail:personal`, `gmail:work`).
- Mounting existing MCP servers as connectors (FastMCP proxy, mapping `readOnlyHint`/`destructiveHint` to read/act) and importing OpenAPI specs.
- Running the browser in a separate process so a hung Chromium can't stall the server.
- Images in tool results for vision models (Discord attachments are currently URLs).
- A Telegram callback webhook (buttons currently open the signed approval page).
