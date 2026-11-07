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
- **Always on.** `pmjc service install` starts it at logon or boot and restarts it if it stops (Windows Task Scheduler, systemd, launchd).

## Quick start

Requires [uv](https://docs.astral.sh/uv/). Python 3.11+ is installed by uv if needed.

```bash
git clone https://github.com/Fwogg3r/PersonalMCPJobController
cd PersonalMCPJobController
uv sync --all-extras          # or: uv sync --extra windows / --extra browser
uv run pmjc setup             # creates your data folder and prints an admin token (once)
uv run pmjc serve             # dashboard at http://127.0.0.1:8770/
```

Paste the admin token into the dashboard. Connect services under **Connectors**, order your models under **Routing**, and create jobs under **Jobs**. When it works, make it permanent:

```bash
uv run pmjc service install
```

Optional:
- Browser: `uv run playwright install chromium`, then `uv run pmjc browser login https://www.messenger.com`.
- Claude on your plan: run `claude setup-token`, then `uv run pmjc auth secret claude.oauth_token`.

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

## Connect your agents

| Client | How |
|---|---|
| Claude Code (this PC) | `claude mcp add pmjc -- uv run --directory /path/to/repo pmjc mcp` |
| Claude Desktop, Cursor, other stdio MCP clients | command `uv`, args `["run", "--directory", "/path/to/repo", "pmjc", "mcp"]` (see [.mcp.json.example](.mcp.json.example)) |
| Any MCP client over HTTP | `http://127.0.0.1:8770/mcp` with header `Authorization: Bearer <token>` |
| claude.ai / Claude mobile apps | publish pmjc through a tunnel and turn on OAuth (see [Remote access](#remote-access)) |
| n8n, Make, Zapier, Home Assistant `rest_command`, iOS Shortcuts | REST: `POST /api/tools/<name>` with a JSON body; OpenAPI at `/openapi.json` |
| OpenAI / Anthropic SDKs, LangChain, LlamaIndex, ... | `GET /api/tools/export/openai` (or `anthropic`), then run each call with `POST /api/tools/<name>` |
| Anything that can send a webhook | `POST /hooks/<name>` creates an event that jobs can react to |

Create tokens with `pmjc token create <name> --scopes sources,jobs` or in the dashboard. The scopes are:
- `sources`: read tools.
- `actions`: tools that change things. Every call still needs your approval.
- `jobs`: create and run jobs.
- `admin`: the dashboard and settings.

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

## Connectors

| Connector | What it gives agents | Needs |
|---|---|---|
| `windows_notifications` | Events and reads from the Windows notification center | Windows; apps signed in on the PC |
| `browser` | Reads pages on allowed domains in pmjc's signed-in browser profile | `--extra browser`, `pmjc browser login <url>` |
| `home_assistant` | States, services, phone location, push notifications with approve buttons | URL + long-lived token |
| `discord` | Read and post in servers your bot is in | Bot token |
| `telegram` | Notifications to you, messages sent to your bot | Bot token + chat id |
| `slack` | Read and post in channels your app joined | Bot token |
| `google` | Gmail search/read/draft, Calendar read/add | Your own Google OAuth client (Desktop app) |
| `github` | Notifications, issue/PR search, comments | Personal access token |
| `weather`, `places` | Open-Meteo forecasts, OpenStreetMap places | Nothing |
| `files` | Read-only list, search and read inside folders you allow | Option `roots` |
| `pc` | CPU, memory, disks, GPU, processes | Nothing |
| `ssh` | Check any machine; run commands with approval | `pmjc ssh add <name> --host ... --user ...` or `~/.ssh/config` aliases |
| `lmstudio` | Model status per host; ask an already-loaded model | Option `hosts` |
| *your CLIs* | Any command-line tool, as YAML (`kind: command`) | A file in `connectors.d/` |
| `ntfy`, `webhook` | Push notifications to your phone, outgoing webhooks | Topic / URL |
| `pmjc` | Events, jobs, runs and approvals | Nothing |

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

Step-by-step guide, including the Claude app connector and an iOS Shortcut: [docs/REMOTE.md](docs/REMOTE.md).

pmjc only listens on `127.0.0.1`. To reach it from a phone or the claude.ai apps:
1. Publish it through a tunnel you control, such as Cloudflare Tunnel or Tailscale Funnel.
2. Add the hostname to `allowed_hosts` and set `public_url` in `settings.yaml`.
3. For claude.ai custom connectors, enable `oauth` with Google or GitHub. pmjc refuses to start OAuth without an `oauth.allowed` list mapping identities to scopes.

**Don't put a browser-login page in front of `/mcp`.** Claude's connectors can't get past one.

## Security model

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The main protections:
- Tool tags fail closed: a tool counts as `act` unless it says otherwise.
- Taint is tracked per run: after a run reads third-party content, egress tools are blocked and every action needs approval.
- Approvals are single-use and tied to a hash of the exact action. A link preview can't approve anything, because approving takes a POST.
- Host and Origin checks block DNS rebinding and cross-site requests.
- Tokens are stored hashed, and the audit log holds metadata only.

Like any keyring or browser profile, anything running as your OS user can read pmjc's secrets. pmjc doesn't protect against malware already on the PC.

## Development

```bash
uv run pytest
uv run pmjc doctor
```

License: MIT.
