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
- **Instances.** A `connectors:` key `<type>:<instance>` (for example `google:work`) runs another copy of the type with its own options. Its id is the whole key, so its secrets are `<type>:<instance>.<field>` (env `PMJC_<TYPE>_<INSTANCE>_<FIELD>`), its state and event sources are namespaced by it, and it is listed, health-checked and usable as a channel under that id. Its tools are `<tool>__<instance>` (instance sanitized to letters, digits, underscores; the base name is shortened to stay within 64 characters) with a `[<instance>] ` description prefix. The bare key stays the default instance with unchanged names. `pmjc auth set <type>:<instance>` and the dashboard (Connectors, Add an instance) create the instance.
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
- **Tool briefs and loading on demand.** A provider with `tool_mode: select` (default `all`) gets the core tools in full, the connectors picked before turn 1 (alias/tag match on the request and trusted thread turns, plus the thread's sticky set) in full, and a one-line brief per other connector in the system prompt. `pmjc_capabilities(task)` loads matching connectors from the next turn (one batch per run, within `select_budget_tokens`, default 30% of the context); a call to an unsent tool still goes through the executor. Code: `tool_select.py`.
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
- **Taint is derived, not declared.** A run is tainted once its trigger event is untrusted or it calls an `untrusted_output` tool. A tainted run can't call `egress` tools, and every act tool goes to approval, even ones on the auto-approve list. One exception: `browser_read`/`browser_extract`/`web_fetch` on a URL the owner declared in the job or watch spec (`read_urls` exact, `read_prefixes` by URL prefix, `read_sites` by domain, never with a query string), since the owner fixed it (`executor.declared_read`; see DECISIONS).
- **Writers need approval after taint.** Tools marked `writes_state` (memory set/delete, contact set, job create/delete, watch start) don't run in a tainted run: the executor files an approval instead, and an approved write is stored with the untrusted label (a job it creates can't act, a watch it starts is read-only). Mounted MCP tools are `act` and need approval anyway.
- **Output egress is defanged.** In a tainted run's answers, job and watch results, and approval previews, links to hosts that aren't trusted are rewritten so they can't be clicked or unfurled (`pmjc.defang`; format in DECISIONS). Discord messages with such answers carry the SUPPRESS_EMBEDS flag, Discord notifications always do, and the dashboard shows answers as text under a CSP that blocks remote images.
- **`sensitive` is derived like `untrusted`.** `Caller.sensitive` is set when a run gets private data: model.md and notes (every ask, job and watch), thread history, approval-result details, and any result of a tool whose `private_output` is true (the default; public-web tools declare false). It is tracked only for now (audit rows `ok` and `run_end` carry `untrusted=.. sensitive=..`, ask results and `ask.history()` report it); a later research sub-run will use it.
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

- **OpenAI-compatible front door** (`openai_door.py`, [FRONT_DOOR.md](FRONT_DOOR.md)): `/v1/models` and `/v1/chat/completions` run `ask()` for any OpenAI-style chat client, with conversation-to-thread mapping, SSE streaming and pending approvals appended to the answer.
- **Chat commands on /v1** (`chat_commands.py`): `/help /tools /connectors /approvals /status /model /threads /new` in the last user message are answered by pmjc with no model run, filtered by the token's scopes, and stored as `command` thread items that `ask.history()` skips. `/model` writes the thread's sticky route and never widens (private stays local).

## Approvals
- **When.** An `act` tool called through **any** agent interface (stdio MCP included) creates a pending approval, unless its permission level is `auto_clean` and the run is clean. Only a person at the CLI or dashboard runs act tools directly.
- **Permission levels** (`pmjc.levels`): `off` (hidden from every tool list and refused), `always_ask` (never auto-approved), `ask` (default for act tools: one approval per call), `auto_clean` (no approval while the run is clean). Precedence: `pmjc_shell_run` and the settings tools are always `always_ask` (only `off` applies to them) > the owner's `tool_levels` in settings.yaml (exact name, else the longest matching glob) > the owner's `auto_approve` lists (global and an owner-written job's own) count as `auto_clean` > the level the tool ships with (`@tool(level=...)`, or `level:` in a YAML connector) > `ask`. Shipped `always_ask`: tools that send free text to other people (`discord_send`, `discord_dm`, `telegram_send`, `slack_send`, `github_comment`), `browser_act`, shell and settings. Shipped `auto_clean`: `lmstudio_load`, `lmstudio_unload`, `command_stop`, `gmail_draft`. Levels other than `off` only matter for act tools: `jobs`-scope tools (threads, notes, contacts, watches) already run without approval in clean runs. `tool_levels` can't be changed from chat. `/api/tools` reports each act tool's effective level.
- **Run grants** (`pmjc.grants`): one approval covers a batch of matching act calls. The model calls `pmjc_grant_request(intent, rules, minutes=30, max_calls=50, cover_tainted=false)` once; pmjc validates the rules, stores a pending grant and files one approval for the internal tool `pmjc_grant` (its stored args, and so the payload hash, hold the rules, cap and cover). The approval shows a summary pmjc builds from the rules (CLOSED/OPEN per rule, expiry, cap, whether it covers runs that read outside content), then the model's intent quoted and defanged. Approving activates the grant; denying marks it denied.
  - **Rules:** `{"tool": name, "args": {arg: constraint}}`, at most 20. Closed: `eq`, `in`, `range`, `template`; open: `re` (fullmatch; match-everything patterns refused), `any`. Arguments a rule doesn't name must be absent or at their default. Command arguments (`command`, `script`, `cmd`) take only `eq`, `in` or `template`. Template slots `<word>`, `<path>` (no `..` segment), `<int>` have fixed character classes without whitespace, quotes or shell metacharacters, and never start with `-` (so a slot can't become an option such as `--force`); literal text in a template matches exactly.
  - **Coverable tools:** act tools whose effective level is `ask` or `auto_clean`; never `always_ask`, `off`, shell or settings.
  - **Binding and run state:** a session grant belongs to `Caller.session_key` (the object holding the taint bits: one MCP session, or one ask/job/watch run), expires, and has a call cap claimed atomically. Run state is clean < derived (consumed a `pmjc_research` result, `Caller.derived`) < untrusted. Open rules cover clean runs only; with `cover_tainted` (all rules closed) a grant also covers derived and untrusted runs. A derived or untrusted run may request closed rules only.
  - **Order in `call_tool`:** the egress-after-taint refusal and the `writes_state`-after-taint approval run first and are never grant-covered. Inside the act block a call that would need approval (or that repeats an act in its thread) looks for a matching grant; a repeat is covered only by a single-valued rule (`eq` on every named argument, or a template without slots). Covered calls are audited `granted` with the grant id; anything else gets the normal per-call approval.
  - **Standing grants:** settings.yaml `grants:` (`name`, `rules`, `cover: clean|untrusted`), owner-written only (hard-denied from the settings tools), no expiry or cap, apply to every agent caller; an invalid one is logged and skipped. None ship by default.
  - **Revocation:** `pmjc grants list|revoke <id>|--all`, `GET /api/grants`, `DELETE /api/grants/{id}`; the kill switch (`/api/kill`, `pmjc token revoke --all`) revokes every session grant.
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

## Settings from chat (`settings_edit.py`)
Decision record: DECISIONS.md, "Configuring pmjc from chat".
- **Tools:** `pmjc_settings_get` (read) lists only allowlisted paths, their current values and allowed direction, a provider summary (id, kind, trust, enabled), the owner's profiles (and which is active) and recent backup ids; never a redacted dump. `pmjc_settings_set`, `pmjc_settings_revert` and `pmjc_profile_switch` are act tools.
- **Who may propose:** only an owner-started chat. A tainted run, a job, a watch or an approval follow-up is refused outright (no approval is created). The tools are never auto-approved (`auto_approve` and the CLI don't help, like `pmjc_shell_run`). At most 3 proposals per hour per caller, and none for 30 minutes after a denial in the same thread.
- **Allowlist (safe direction only, with bounds):** `router.*` tuning (sticky, footer, gpu_busy_action `ask|local`, gpu_busy_jobs `wait|local`, backoff and wait bounds, stay_*), `routing[id].enabled` (to false only), `schema_mode`, `tool_mode`, `select_budget_tokens`/`max_result_chars` (lower only), `condense_at`, `monthly_budget_usd` (lower only, never null), `research.max_turns`/`max_chars` (lower only), `front_door.stream_progress`/`history_turns`, `commands.wait_seconds`, `approval_wait_seconds`, `approval_outcome_notify` (to true), `thread_retention_days` (up only, at most 365), `result_ttl_hours` (down only), `monitoring_enabled` and `scheduled_jobs_enabled` (to false), `auto_start_local`, `disabled_connectors` (add only, never `pmjc` or a notify channel). A proposal changes at most 3 paths of one risk class (`off-lower`, `routing`, `cost`).
- **Hard deny (in code, checked first):** auto_approve, tool_levels, grants, hosts, ports, public_url, credentials, oauth, plugins, mcp_servers, connectors.*, discord_door, notify_channels, monitoring_sources, shell_command_*, output_link_allowlist, audit and approval timing, research.tools, front_door.enabled/limits, gpu_busy_processes, profiles, and for providers the id, kind, base_url, model, credential, start, trust, capabilities, context and price fields, extra and load_model. Paths use `routing[<id>].field` for providers.
- **Approval view:** a risk badge, one line per path (`old → new`, lists as `+added −removed`), values escaped (no Markdown, mentions suppressed, zero-width and bidi characters stripped), the model's reason quoted last as "model's claim". Discord, the approval link page and the dashboard share it. Routing and cost changes take a two-step confirm (Discord: Review, then Approve; link page: a review page, then the form; dashboard: a second click).
- **Applying (after approval):** the proposal stores the sha256 of `settings.yaml`, so the approval hash covers it. Under a cross-process lock (`config.file_lock`, an O_EXCL lockfile that every settings writer takes) the file is re-read and compared; a mismatch fails with "settings changed, propose again". Only the changed keys are patched with ruamel (comments kept, no defaults written; the built-in provider list is written out once if a provider is edited and the file has no `routing`), the whole model and invariants are validated (tier references, trust of providers unchanged, budgets never become unlimited, at least one provider enabled), the old file goes to `<data>/settings-backups/` (last 20; the files connector never reads that folder), and the file is replaced atomically. `config.load()` reads the file on every call, so other processes see it at once. The audit records base and new hashes, approver, surface, thread and one row per path.
- **Revert:** `pmjc_settings_revert(backup_id)` diffs the current file against the backup, limited to allowlisted paths. If any other path differs it refuses and points to the dashboard; budgets are never raised by a revert.
- **Profiles:** `profiles: {name: {path: value}}` in `settings.yaml` (written by the owner, never from chat). Example: `gaming: {router.gpu_busy_action: ask, "routing[local-model].enabled": false}`. Profile values may move allowlisted paths in either direction, never hard-denied ones; a bad profile is refused when listed or switched to. `pmjc_profile_switch(name)` shows the resulting diff and applies it with the same lock, compare-and-swap and backup.

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
