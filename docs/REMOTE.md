# Remote access: phone, Claude apps, other tools

pmjc listens on `127.0.0.1` only. To reach it from elsewhere, publish it through a tunnel you control and choose how clients authenticate.

| Client | Auth | Endpoint |
|---|---|---|
| Claude iOS/Android/web/desktop (custom connector) | OAuth (Google or GitHub), identity allowlist | `https://<host>/mcp` |
| iOS Shortcuts, other dashboards, scripts | pmjc token (`Authorization: Bearer`) | `POST https://<host>/api/ask`, `/api/tools/<name>`, `/api/watches/<id>` |
| Other agent CLIs with remote MCP (Codex, Gemini CLI, Cursor, ...) | pmjc token header | `https://<host>/mcp` |
| Discord (any device, no tunnel needed) | Your Discord user id, checked by the bot | Your bot, in your own server or its DMs ([section 6](#6-chat-from-discord)) |

## 1. Publish through a tunnel (Cloudflare example)
1. In Cloudflare, open Networking → Tunnels → your tunnel → Routes → Add route → Published application.
2. Enter your hostname (e.g. `pmjc.example.com`) and service `http://localhost:8770`.
3. **Don't put a Cloudflare Access login app on this hostname.** Claude's connectors can't complete a browser login. pmjc does its own authentication: OAuth for Claude, tokens for everything else.

Tailscale Funnel or any reverse proxy works the same way.

## 2. Tell pmjc its public address
```bash
uv run pmjc remote https://pmjc.example.com --allow you@example.com --scopes sources,jobs,actions
```
This sets `public_url` and `allowed_hosts` and turns on OAuth for that one identity. Every other account is refused. Act tools still need approval, whatever the scopes.

## 3. OAuth client (for the Claude apps)
1. **Sign in to Google Cloud console with the account you allowed in step 2.** If you have several Google accounts, check the avatar in the top right; a project made under another account works but can't use the Internal audience.
2. **Create a project** (e.g. `pmjc`). With a Google Workspace account, pick your organization.
3. **Configure the sign-in screen** (Google Auth Platform → Get started):
   - App name `pmjc`, support email and contact email: your address.
   - Audience: **Internal** if you have Workspace (only your organization's accounts can sign in, and no Google review is needed). With a personal Gmail account choose External, leave it in Testing and add yourself as a test user.
   - Agree to the Google API Services User Data Policy and create.
4. **Create the client** (Clients → Create client): type **Web application**, any name, authorized redirect URI `https://pmjc.example.com/auth/callback`. No JavaScript origins.
5. **Copy the client ID and secret** from the dialog (or Download JSON). The secret is only shown once; you can add a new one on the client's page later.
6. **Store them in pmjc** on the machine and Windows account that runs the service, either on the dashboard (Routing → Provider keys → `oauth.client_id` and `oauth.client_secret`) or with:
   ```bash
   uv run pmjc auth secret oauth.client_id
   uv run pmjc auth secret oauth.client_secret
   ```
   The prompt hides what you type, so a paste can silently arrive empty. In a Windows terminal, paste with a right-click. `pmjc auth list` shows what's stored.
7. **Restart the service:** `schtasks /End /TN pmjc` then `schtasks /Run /TN pmjc`. On other systems, restart the service your own way. The log says `oauth.enabled but ... missing` if the client didn't get stored. Check that `https://pmjc.example.com/.well-known/oauth-authorization-server` now returns JSON.
8. **Add the connector** in claude.ai: Customize → Connectors → Add connector → Add custom connector. Name `pmjc`, URL `https://pmjc.example.com/mcp`, keep the default authentication (sign in, dynamic client registration). Then click **Connect** and sign in with the allowed account. The connector then works in the Claude web, desktop and mobile apps. New Google clients can take a few minutes before sign-in works.

Until steps 3 and 4 are done, `/mcp` accepts pmjc tokens only.

## 4. iOS Shortcut ("Ask pmjc")
1. Create a token for the phone: `uv run pmjc token create iphone --scopes sources,jobs`, or use the dashboard's Tokens tab.
2. Build a new Shortcut:
   1. **Ask for Input** (Text): "What do you need?"
   2. **Get Contents of URL:**
      - URL: `https://pmjc.example.com/api/ask`
      - Method: POST
      - Headers: `Authorization` = `Bearer <token>`
      - Request Body: JSON, `text` = *Provided Input*
   3. **Get Dictionary Value** `answer` from *Contents of URL*.
   4. **Show Result.**
3. Optional: add it to the Home Screen, or say "Hey Siri, Ask pmjc".

If the request needs waiting, the answer says a watch was started. The result arrives later on your notification channel (ntfy, Home Assistant or Telegram), or at `GET /api/watches/<id>`.

`/api/ask` needs a model in your routing list: a loaded local model (or auto-start on), a Claude plan token, or an API key. Clients that bring their own model, like the Claude app or agent CLIs, don't need one.

## 5. Optional extra layer
To keep `/api/*` off the open internet, put it behind WARP. Route the hostname through the device client's split tunnel, and have the Access policy require an enrolled device. Clients still send their pmjc token. `/mcp` must stay reachable for Claude's servers.

## 6. Chat from Discord

The Discord front door lets you talk to pmjc from the Discord app on any device. The service keeps an outbound connection to Discord, so this works without a tunnel or `public_url`.

1. **Make a server** just for you (Discord → "+" → Create My Own). Add a text channel such as `#pmjc`. A second channel such as `#pmjc-alerts` can take job and watch results.
2. **Make the bot** at <https://discord.com/developers/applications> → New Application (the name is the bot's display name). Discord may show a CAPTCHA. Then on the **Bot** page:
   - Turn on **Message Content Intent** under Privileged Gateway Intents and save. The other two intents aren't needed.
   - Optional: turn off **Public Bot** so only you can add it. If Discord refuses, first set Installation → Install Link to None. The door ignores everyone but you either way.
   - **Reset Token** and copy it (it's shown once).
3. **Add the bot to your server.** Open this URL with your application id (General Information → Application ID) and server id filled in, and authorize:
   ```
   https://discord.com/oauth2/authorize?client_id=<application id>&scope=bot%20applications.commands&permissions=326417583104&guild_id=<server id>
   ```
   The permissions number is View Channels, Send Messages, Read Message History, Create Public Threads, Send Messages in Threads and Manage Threads. On a PC the link may hand off to the Discord desktop app; authorizing there or on a phone works too.
4. **Get the ids:** Discord Settings → Advanced → Developer Mode on. Then right-click (long-press on a phone) yourself → Copy User ID, and each channel → Copy Channel ID.
5. **Store the token** on the machine and account that runs the service:
   ```bash
   uv run pmjc auth set discord
   ```
   It also asks for `notify_channel_id`: give the alerts channel's **numeric id**, not its `#name`, or leave it blank and run `/pmjc notify here` in that channel later. Put `discord` in `notify_channels` in `settings.yaml` (`/pmjc notify here` does both).
6. **Turn it on and restart the service:**
   ```bash
   uv run pmjc discord-door setup --owner <your user id> --channel <#pmjc channel id>
   ```
   Add `--private` to keep these conversations on local models. To give the control command another name than `/pmjc`, set `discord_door.command_name` in `settings.yaml`.
7. **Check it:** the service log shows `discord door connected as <bot>` and `commands registered`. In Discord, `/pmjc status` lists the door and which models are up.
8. **Give it a model.** If `/pmjc status` shows every provider down, either let pmjc start your local model (`/pmjc switch auto_start_local on`; pmjc then runs LM Studio's `lms server start` and loads a model when needed) or connect `claude -p`: run `claude setup-token`, sign in, and store the printed token with `uv run pmjc auth secret claude.oauth_token`. Don't put that token in an environment variable, where Claude Code would pick it up as well.

How it works:
- **Conversations.** Each message in `#pmjc` starts a Discord thread and a pmjc thread. Reply in that Discord thread to continue; the last few exchanges go back to the model. DMs to the bot work too, one conversation per DM until you send `new`.
- **Threads.** A message in any Discord thread under a door channel, including one you create in the Discord UI, starts its own pmjc thread on the first message. Deleting the Discord thread closes the pmjc thread.
- **Slash commands** (guild commands, registered when the bot connects; owner only, everyone else gets an ephemeral refusal):

  | Command | Does |
  |---|---|
  | `/tool name args` | Runs a tool directly with the door's scopes, no model and no cost. Act tools still post Approve/Deny buttons. `args` is a JSON object. |
  | `/pmjc new title` | New thread in the door channel, bound to a new pmjc thread |
  | `/pmjc close` / `delete` | Archive and lock this thread / delete it after a Confirm button; the pmjc thread is closed |
  | `/pmjc trace on\|off` | One `-# tool name(args) -> status` line per tool call in this thread (never results). Default: `discord_door.trace` |
  | `/pmjc model provider` / `private on\|off` | Provider (`auto` or a routing id) and local-only for this thread |
  | `/pmjc autoapprove add\|remove\|list` | Edit `auto_approve`: those act tools then run without a button, for every caller |
  | `/pmjc jobs list\|run\|enable\|disable`, `watches list\|cancel` | Manage jobs and watches |
  | `/pmjc notify here` | Send pmjc notifications to this channel, Discord first |
  | `/pmjc switch name on\|off` | `monitoring`, `scheduled_jobs`, `auto_start_local` |
  | `/pmjc friend add name user_id` / `list` | Remember a Discord user as a contact |
  | `/pmjc status` | Door, providers, pending approvals |
- **Friends.** The model knows it can message someone with `discord_dm` (sent as the bot, only to people who share a server with it) or `pmjc_handoff` (a one-tap link so you send it as yourself). Both are act tools, so each needs your approval.
- **Who it answers.** Only the owner ids you set. pmjc never reads anyone else's messages, even in the same channel.
- **Approvals.** Act tools need approval, as they do for every remote caller. The bot posts Approve/Deny buttons in the conversation, and only an owner's click counts. While the door is connected, other approval notifications sent to Discord use buttons too, so they need no public URL.
- **Models.** Requests go through the normal routing: local first, then `claude -p`, then paid APIs if you enabled them. The reply footer names the model that answered.
- **Thread titles.** A thread the door starts from a message is first named after the message, then renamed to a 2–6 word topic by the model that answered (one extra short call, no tools), like chat apps do. Threads you create or name yourself, `/pmjc new` threads and DMs keep their names. Turn it off with `discord_door.auto_title: false`.
- **Run log.** With `--log-channel <channel id>` (or `discord_door.log_channel_id`), every message, slash command and button click posts its pmjc log lines there: the request, tool calls (names and arguments, never results), the provider that answered and any provider it fell back from, with known secrets scrubbed. Replies also end with a `fell back from ...` line when the first choice failed.
- **Status:** `pmjc discord-door status` shows the settings and anything missing. Whether the bot is connected: `GET /api/discord-door` with an admin token, or the service log.

Trust: anyone who controls your Discord account can use pmjc with the scopes you gave the door (default `sources,jobs,actions`; actions still need a button click). Turn on Discord two-factor sign-in, or narrow the door with `--scopes sources,jobs`.
