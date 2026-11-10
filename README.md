# pmjc: Personal MCP Job Controller

**pmjc** is a self-hosted switchboard between AI agents and the services you use. You connect your accounts once: notifications, Discord, Telegram, Slack, Gmail, Calendar, Home Assistant, GitHub, a signed-in browser, any HTTP API. Then every agent and automation tool can use them through one place. pmjc also runs **jobs**: plain-English prompts on a schedule or triggered by events, using your local models first and the cloud only when needed.

```
Claude / Cursor / ChatGPT-style agents ──► MCP (stdio or HTTP)  ┐
n8n · Home Assistant · iOS Shortcuts   ──► REST + OpenAPI       ├─► pmjc ─► your services
LangChain · OpenAI/Anthropic SDKs      ──► tool-schema export   │          + scheduled/event jobs
you                                    ──► dashboard · CLI      ┘          + model router (local → plan → API)
```

- **One place for credentials.** Enter an API key, a token, an OAuth sign-in or a browser login once, in the dashboard or the CLI. Secrets are encrypted at rest and never returned by any API.
- **Every interface from one registry.** Add a connector and its tools appear in MCP, REST/OpenAPI, the CLI, the dashboard and the tool exports at once.
- **Modular.** A new service is one small Python file, or a YAML file if it's a plain HTTP API.
- **PC-native sensing.** On Windows, pmjc reads the notification center, so any app that notifies you (Discord, Messenger in Chrome, Outlook, ...) becomes an event source. It can hide those banners so only pmjc sees them.
- **Safe by default.** Tools that change things need your approval (dashboard, phone notification or link). Runs that read third-party content can't send data out. Tokens are scoped, every call is audited, and there's a kill switch.
- **One chat app for everything.** Point any OpenAI-compatible chat app, on your phone or PC, at pmjc's `/v1` endpoint. pmjc picks the model (local first), the tools and the thread for each message ([docs/FRONT_DOOR.md](docs/FRONT_DOOR.md)). Chat commands (`/tools`, `/connectors`, `/approvals`, `/status`, `/model`, `/threads`, `/new`) are answered by pmjc itself, with no model run.
- **Built for local models.** Compact tool schemas, one-line tool briefs with on-demand loading, bounded tool results with paging, automatic condensing near the context limit, and a quarantined web-research sub-run keep a 32k-64k model useful. `pmjc eval` measures whether a change actually helps.
- **Reuse existing MCP servers.** Mount any MCP server (GitHub, Spotify, notes apps, ...) as a connector; pmjc's approvals and taint rules apply to its tools too.
- **Always on.** `pmjc service install` starts it at logon or boot and restarts it if it stops (Windows Task Scheduler, systemd, launchd).

## Set up with an AI agent

Open a coding agent (Claude Code, Codex, Cursor, Gemini CLI, ...) in an empty folder and paste:

> Clone https://github.com/Fwogg3r/PersonalMCPJobController, then follow its AGENTS.md section "Setting it up for a user" to install pmjc on this machine, run it as a service, and connect yourself to it over MCP. Give me the admin token privately, and tell me which services I can connect next. Don't enter any of my passwords or API keys yourself; I'll type them into the dashboard.

[AGENTS.md](AGENTS.md) gives the agent the exact commands, the checks to run, and the rules: secrets are yours to enter, and nothing turns on background activity unasked.

## Set up by hand

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.11+ if needed) and git. Windows 11 is fully supported; Linux and macOS run everything except the Windows notification connector.

```bash
git clone https://github.com/Fwogg3r/PersonalMCPJobController
cd PersonalMCPJobController
uv sync --all-extras          # Windows. Linux/macOS: uv sync --extra browser
uv run pmjc setup             # creates your data folder and prints an admin token (shown once)
uv run pmjc doctor            # checks Python, extras, notification access, model providers
uv run pmjc serve             # dashboard at http://127.0.0.1:8770/  (Ctrl+C to stop)
```

Paste the admin token into the dashboard. Then:
- connect services under **Connectors**;
- order your models under **Routing**;
- choose what may run on its own under **Settings**. Everything starts off.

When it works, make it permanent:

```bash
uv run pmjc service install   # starts at logon/boot and restarts itself; `pmjc service status` to check
```

Connect your agents with the table in [Connect your agents](#connect-your-agents). For Claude Code on the same machine:

```bash
claude mcp add pmjc -- uv run --no-sync --directory "$PWD" pmjc mcp
```

Optional:
- **Browser:** `uv run playwright install chromium`, then `uv run pmjc browser login https://www.messenger.com`.
- **Claude on your plan for jobs:** run `claude setup-token`, then `uv run pmjc auth secret claude.oauth_token`.
- **Your other machines:** `uv run pmjc ssh add nas --host 192.0.2.10 --user admin --key ~/.ssh/id_ed25519`.
- **Your own command-line tools:** copy [examples/connectors.d/command-tools.yaml](examples/connectors.d/command-tools.yaml) into `<data folder>/connectors.d/` and edit it.
- **Phone and claude.ai access:** see [docs/REMOTE.md](docs/REMOTE.md).

Where things live:
- **Code:** this folder.
- **Your data:** `%USERPROFILE%\.pmjc` on Windows, `~/.local/share/pmjc` elsewhere, or `PMJC_HOME`. That's settings, encrypted credentials, jobs, browser profile, logs and database.
- **Updating:** `git pull && uv sync --all-extras`, then restart the service.
- **Removing:** `uv run pmjc service uninstall`, then delete both folders.

## Nothing runs unless you ask

Out of the box, pmjc does nothing in the background. It reads data only when you or an agent call a tool, or when you run a job by hand. Three switches on the dashboard's **Settings** tab change that, and all three start off:

| Switch | When on |
|---|---|
| Background monitoring (per source) | The ticked sources poll in the background (e.g. new Windows notifications every 3 s) and store events |
| Scheduled and event-triggered jobs | Jobs run on their cron, interval or event triggers. Manual runs always work |
| Auto-start local models | A run that needs a local model may start the model server and load the model. When off, local hosts are used only if the model is already loaded |

The **Settings** tab also shows what's polling right now and lets you delete stored events.

## Automate what you keep asking

- **On the Ask tab:** under any request, **Run automatically** turns it into a job: every N minutes, or daily at a time. By default it only notifies you when there's something new. **Requests you repeat** lists what you've asked more than once, each with an **Automate** button.
- **On the Jobs tab:** **Turn on / Turn off** each job. Scheduled jobs also need the **Scheduled jobs** switch on the Settings tab; automating a request turns it on for you.
- **On the Settings tab:** **Run actions without asking** lets you tick actions (e.g. `home_switch`) that may run without an approval prompt. They still ask whenever the run has read third-party content.
- **The same over the API:** `POST /api/asks/automate`, `PUT /api/jobs/<name>/enabled`, `PUT /api/auto-approve`.

## Requests and watches

Every request follows the same flow:

1. **You ask.** Use any agent connected over MCP, `POST /api/ask` (phone, other apps), `pmjc ask "..."`, or the dashboard's **Ask** tab.
2. **The agent checks what's connected** (`pmjc_connectors`).
3. **It answers right away** if the connected tools have what it needs.
4. **If the answer needs waiting, it starts a watch:** a temporary monitor for that one request.

   | Type | Fires when | Example |
   |---|---|---|
   | `time` | At a time, or after a delay | "Check the weather again at 6" |
   | `event` | A matching event arrives. The source, e.g. Windows notifications, is polled only while the watch lives | "Tell me when Ann replies" |
   | `poll` | A read tool, called every N seconds, returns output that matches or changes. An optional yes/no question goes to a local model | "Tell me when the Discord thread mentions the meetup" |

5. **When the watch fires,** pmjc answers your request once with what it gathered and sends the answer to your notification channel. It's also readable at `GET /api/watches/<id>`. Then the watch ends and any source it turned on is turned off again.

Watches expire (default 24 h, at most 7 days), can be cancelled, and survive restarts. They don't need global monitoring, and events seen only for a watch aren't stored. Agents use `pmjc_watch_start`, `pmjc_watch_status`, `pmjc_watch_wait`, `pmjc_watch_cancel` and `pmjc_watch_list`.

`/api/ask` and `pmjc ask` need a model, picked from your routing list. Agents connected over MCP bring their own model.

## Context across sessions

Agent sessions are short and forget everything. pmjc is the one place they share, so a fresh session from **any** vendor's model, using only pmjc's tools, can pick up where the last one stopped.

- **Threads.** Every request, watch and handoff lives in a thread (`t_xxxxxx`) holding the request, answers, notes, watch results, handoffs and approval outcomes. A watch that fires after the session ended marks its thread *updated*; the notification ends with `Resume: thread t_xxxxxx`. Any session resumes it with `pmjc_thread_get`. `/api/ask` and `pmjc ask --thread` take and return `thread_id` too.
- **`model.md`.** Your standing rules, created from a template in the data folder on first run and editable in the dashboard (Context tab). It is given to every model: in the MCP server instructions, at the top of each MCP session's first tool result, by `pmjc_context`, and in the system prompt of `/api/ask`, jobs and watches (capped at 8 KB).
- **Shared notes and contacts.** `pmjc_memory_*` keeps durable facts; `pmjc_contact_*` keeps people with aliases and per-service handles (the display name each app shows, ids, phone, email) so "Sam" can be matched to what Discord displays.
- **Capability awareness.** Each connector declares its limits and what a client with better abilities should use; they are appended to every tool description. `pmjc_capabilities(task)` gives the full picture, including the closest workaround ([docs/CAPABILITIES.md](docs/CAPABILITIES.md)).
- **Handoff.** When only you can do the last step (send as yourself, call), `pmjc_handoff` notifies you with a one-tap deep link (Discord DM, SMS or WhatsApp with the text filled in, email, call), records it in the thread and can watch for the reply.
- **Safe by construction.** Every item has a trust tier: *owner* (you), *agent* (a clean session) or *untrusted* (third-party content, or anything written by a session that had read some). Context given to a session contains owner text verbatim, agent text labelled, and untrusted text never: only "watch result available". Sessions that read third-party content can't write notes or contacts, agents can't overwrite your notes, contacts proposed by agents wait for your approval, and an action already done in a thread asks for approval again before it repeats. Closed threads are deleted after `thread_retention_days` (default 90).

How it works from any client: connect, read the first result's context header (or call `pmjc_context`), `pmjc_thread_get` the thread if the request continues one, and `pmjc_thread_note` what you did. MCP clients that support prompts also get `resume_thread`.

## Connect your agents

| Client | How |
|---|---|
| Claude Code (this PC) | `claude mcp add pmjc -- uv run --no-sync --directory /path/to/repo pmjc mcp` |
| Claude Desktop, Cursor, other stdio MCP clients | command `uv`, args `["run", "--directory", "/path/to/repo", "pmjc", "mcp"]` (see [.mcp.json.example](.mcp.json.example)) |
| Any MCP client over HTTP | `http://127.0.0.1:8770/mcp` with header `Authorization: Bearer <token>` |
| claude.ai / Claude mobile apps | publish pmjc through a tunnel and turn on OAuth (see [Remote access](#remote-access)) |
| Any OpenAI-compatible chat app (phone or PC) | base URL `https://<your pmjc host>/v1`, API key = a pmjc token; models `pmjc`, `pmjc-local`, `pmjc:<tier>` ([docs/FRONT_DOOR.md](docs/FRONT_DOOR.md)) |
| n8n, Make, Zapier, Home Assistant `rest_command`, iOS Shortcuts | REST: `POST /api/tools/<name>` with a JSON body; OpenAPI at `/openapi.json` |
| OpenAI / Anthropic SDKs, LangChain, LlamaIndex, ... | `GET /api/tools/export/openai` (or `anthropic`), then run each call with `POST /api/tools/<name>` |
| Anything that can send a webhook | `POST /hooks/<name>` creates an event that jobs can react to |

Create tokens with `pmjc token create <name> --scopes sources,jobs` or in the dashboard. The scopes are:
- `sources`: read tools.
- `actions`: tools that change things. Every call still needs your approval.
- `jobs`: create and run jobs.
- `admin`: the dashboard and settings.

When an MCP client calls a tool that needs your approval, the call waits (up to `approval_wait_seconds`, default 50 s) and returns the real result once you approve; if you take longer, the model is told to call `pmjc_approval_wait` and keeps waiting on its own, so you never have to prompt it again. Long commands (`ssh_run`, `pmjc_shell_run`) return within a few seconds with a run id; the model follows up with `command_status`.

## Jobs

A job is a small YAML file in your data folder (`pmjc jobs examples --copy` gives you starters):

```yaml
name: marketplace-replies
trigger:
  event: {source: windows_notifications}   # or cron: "*/15 * * * *" | interval: 600 | webhook | manual
match: {app: ["Messenger", "Google Chrome"], regex: "(?i)marketplace|still available"}
prompt: |
  If this notification is a Marketplace seller replying to me, summarize who and what in one line.
  Otherwise answer exactly NOTHING.
scopes: [sources]
model: {private: false}       # private: true = local models only, never the cloud
notify: [ntfy]                # where results go; NOTHING means no notification
```

Agents can create jobs too (`pmjc_job_create`), but they can't give a job more access than they have themselves.

## Model routing

Jobs run on the first provider in your list (dashboard, **Routing**) that is enabled, healthy, within budget and has the capabilities the job needs. The default order is:
1. **Local OpenAI-compatible hosts** (LM Studio, Ollama, ...). pmjc starts the server if it's down and loads the model (`lms load`). It can skip local hosts while the GPU is busy.
2. **Claude Code on your plan** (`claude -p`), locked down: its own config folder, no built-in tools, only pmjc's MCP server, and no API key in its environment.
3. **Paid APIs** (Anthropic, OpenAI, OpenRouter or any OpenAI-compatible URL), each with a monthly budget cap.

Each run stays on one provider. If that provider fails before anything was changed, the run starts over on the next one.

**Front-door routing** (chats from Discord, `/v1`, REST and the CLI):
- **Tiers and rules.** Named provider lists (`default`, `strong`, `local`, or your own) and `route_rules` (prefix such as `/strong`, keywords, surface, length); the first match wins. Hard limits come first: private threads stay local, and routing never picks a provider outside the allowed candidates.
- **Sticky per thread.** A thread keeps the provider chosen on its first turn. If it fell back (say the local model was still loading), the next turn switches back unless the thread has become hard or long; the answer footer says which and how to pivot (`/local`, `/strong`).
- **Waiting for a local start.** With `auto_start_local` on, a request that can only run locally (private thread, `/local`) starts the host or loads its model and waits until that start finishes (at most `router.local_start_wait_seconds`, 600), streaming a progress line every `router.local_start_notice_seconds` (15); private work runs only on the providers in the `local` tier (`router.tiers.local`, by default every local provider); `router.prefer_local_wait_seconds` (0) optionally waits before falling back to the cloud. The footer notes "waited Ns for local start".
- **GPU busy.** List your games or launchers in `router.gpu_busy_processes`. When the GPU is busy, pmjc asks: use local anyway (approval), use a cloud model, or wait. Jobs wait by default.
- **Tools for small models.** Per provider, `schema_mode: compact` and `tool_mode: select` send a core set plus the tools the request needs, with one-line briefs of the rest; the model loads more with `pmjc_capabilities`.
- **Seeing what a local model does.** OpenAI-compatible providers stream (provider `stream: true`): while the model reads a long prompt, chat apps get "_model reading the prompt (~12k tokens, ~40%)… 30s_" lines (percent from the model's past speed, or from LM Studio's server log with `extra.progress_log`); then the answer streams as it is written and the model's thinking arrives as `reasoning_content` (`front_door.reasoning`: `field`, `think_tags` or `off`). Answers of runs that read untrusted content are sent whole after the link check. `pmjc completions` lists every model call: prompt size, tools sent, time to first token, output speed, errors. For a big model on one GPU, set the provider's `parallel: 1` so `lms load` doesn't reserve memory for 4 parallel requests.
- **Routing log.** `pmjc routing --days 7` shows what was chosen and why, and which answers you re-asked on a stronger tier.

## Connectors

| Connector | What it gives agents | Needs |
|---|---|---|
| `windows_notifications` | Events and reads from the Windows notification center | Windows; apps signed in on the PC |
| `browser` | Reads pages (text and links), extracts rows by CSS selector, scrolls and expands; clicking and typing (`browser_act`) need approval. Allowed domains only, signed-in profile | `--extra browser`, `pmjc browser login <url>` |
| `web` | `web_search` and `web_fetch` (readable text, private addresses refused); `pmjc_research` runs multi-page research in a sub-run that never sees your private data | A SearXNG URL or a Brave Search key |
| `ebay`, `craigslist` | Structured listing search, several queries per call | eBay developer keys; Craigslist areas |
| `home_assistant` | States, services, phone location, push notifications with approve buttons | URL + long-lived token |
| `discord` | Read and post in servers your bot is in | Bot token |
| `telegram` | Notifications to you, messages sent to your bot | Bot token + chat id |
| `slack` | Read and post in channels your app joined | Bot token |
| `google` | Gmail search/read/draft, Calendar read/add | Your own Google OAuth client (Desktop app) |
| `github` | Notifications, issue/PR search, comments | Personal access token |
| `weather`, `places` | Open-Meteo forecasts, OpenStreetMap places | Nothing |
| `files` | Read-only list, search and read inside folders you allow | Option `roots` |
| `pc` | CPU, memory, disks, GPU, processes | Nothing |
| `ssh` | Check any machine and run commands with approval, including the machine pmjc runs on (no SSH needed for it) | `pmjc ssh add <name> --host ... --user ...` or `~/.ssh/config` aliases |
| `lmstudio` | Model status per host; ask an already-loaded model | Option `hosts` |
| *your CLIs* | Any command-line tool, as YAML (`kind: command`) | A file in `connectors.d/` |
| `ntfy`, `webhook` | Push notifications to your phone, outgoing webhooks | Topic / URL |
| `pmjc` | Events, jobs, runs, approvals, threads, memory, contacts; `pmjc_shell_run` as a last resort (always approved) | Nothing |
| *mounted MCP servers* | Every tool of any MCP server you list in `mcp_servers:` (stdio or HTTP), as `<id>__<tool>`; tools need approval unless the server marks them read-only | The server's own setup |

Run a connector more than once with `<type>:<instance>` keys (for example `google:work` next to `google`); the extra instance's tools get a `__work` suffix.

### Add a service

- **Plain HTTP API:** drop a YAML file in `<data folder>/connectors.d/` (see [examples/connectors.d](examples/connectors.d)).
- **Anything else:** one Python file in `<data folder>/plugins/` (see [examples/plugins/template.py](examples/plugins/template.py)), or a package that exposes a `pmjc.connectors` entry point. Entry-point packages only load if you list them in `enabled_plugins`.

The contract fits on one screen: list the credential `Field`s, mark methods with `@tool(scope="read"|"act", untrusted_output=..., egress=...)`, and optionally add an `@event_source` and a `notify()` method. See [docs/CONNECTORS.md](docs/CONNECTORS.md).

## Windows notifications without the pop-ups

Sign in to each service on the PC: Discord desktop, `messenger.com` in Chrome with notifications allowed, and so on. Then:

```bash
uv run pmjc notifications apps                 # which apps notify, and whether their banners show
uv run pmjc notifications silence com.squirrel.Discord.Discord
```

`silence` keeps the app's notifications going to the notification center, where pmjc reads them, but hides the banners. If your Windows build stores the setting differently, run `pmjc notifications learn <app>` once and toggle the setting by hand; pmjc records the exact values.

Chrome sends notifications from every site under one app ID. To silence just one site, install it as an app ("Install page as app") or use a separate Chrome profile for it.

## Remote access

Step-by-step guide, including the Claude app connector, an iOS Shortcut and chatting with pmjc through your own Discord bot: [docs/REMOTE.md](docs/REMOTE.md).

pmjc only listens on `127.0.0.1`. To reach it from a phone or the claude.ai apps:
1. Publish it through a tunnel you control, such as Cloudflare Tunnel or Tailscale Funnel.
2. Add the hostname to `allowed_hosts` and set `public_url` in `settings.yaml`.
3. For claude.ai custom connectors, enable `oauth` with Google or GitHub. pmjc refuses to start OAuth without an `oauth.allowed` list mapping identities to scopes.

**Don't put a browser-login page in front of `/mcp`.** Claude's connectors can't get past one.

## Security model

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The main protections:
- Tool tags fail closed: a tool counts as `act` unless it says otherwise.
- Taint is tracked per run: after a run reads third-party content, egress tools are blocked and every action needs approval. Writing memory, contacts, jobs or watches from such a run also needs approval, and links in its answers are defanged so they can't leak data.
- A derived `sensitive` flag tracks whether a run has seen your private data; only the web-research sub-run, which starts with nothing private, may keep browsing after reading web pages.
- Approvals are single-use and tied to a hash of the exact action. A link preview can't approve anything, because approving takes a POST.
- Host and Origin checks block DNS rebinding and cross-site requests.
- Tokens are stored hashed, and the audit log holds metadata only.
- Chat can change a few settings in the safe direction (turn things off, lower limits, switch your own profiles) with your approval and a diff; credentials, hosts, approvals and spend limits upward stay in `settings.yaml` or the dashboard. See "Settings from chat" in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Like any keyring or browser profile, anything running as your OS user can read pmjc's secrets. pmjc doesn't protect against malware already on the PC.

## Development

```bash
uv run pytest
uv run pmjc doctor
uv run pmjc eval run      # score the assistant on scripted tasks with your local model (see docs/EVALS.md)
```

License: MIT.
