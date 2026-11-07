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
1. In Google Cloud console → APIs & Services → Credentials → Create credentials → OAuth client ID, choose type **Web application**.
2. Add the authorized redirect URI `https://pmjc.example.com/auth/callback`.
3. Store the client in pmjc, either on the dashboard (Routing → Provider keys → `oauth.client_id` and `oauth.client_secret`) or with:
   ```bash
   uv run pmjc auth secret oauth.client_id
   uv run pmjc auth secret oauth.client_secret
   ```
4. Restart the service: `schtasks /End /TN pmjc` then `schtasks /Run /TN pmjc`. On other systems, restart the service your own way.
5. In the Claude app, go to Settings → Connectors → Add custom connector and enter URL `https://pmjc.example.com/mcp`. Sign in with the allowed Google account once; the connector keeps a refresh token.

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
2. **Make the bot** at <https://discord.com/developers/applications> → New Application → Bot:
   - Reset the token and copy it.
   - Turn on **Message Content Intent** under Privileged Gateway Intents.
   - Under OAuth2 → URL Generator, tick `bot`, then the permissions *View Channels*, *Send Messages*, *Send Messages in Threads*, *Create Public Threads*, *Manage Threads* (for `/fwog close` and `delete`) and *Read Message History*. Also tick the `applications.commands` scope so the slash commands appear. Open the URL and add the bot to your server.
3. **Store the token:** `pmjc auth set discord` (or the dashboard). Set `notify_channel_id` to the alerts channel if you want results there, and add `discord` to `notify_channels` in `settings.yaml`.
4. **Get the ids:** Discord Settings → Advanced → Developer Mode on. Then right-click yourself → Copy User ID, and right-click `#pmjc` → Copy Channel ID.
5. **Turn it on and restart the service:**
   ```
   pmjc discord-door setup --owner <your user id> --channel <#pmjc channel id>
   ```
   Add `--private` to keep these conversations on local models.

How it works:
- **Conversations.** Each message in `#pmjc` starts a Discord thread and a pmjc thread. Reply in that Discord thread to continue; the last few exchanges go back to the model. DMs to the bot work too, one conversation per DM until you send `new`.
- **Threads.** A message in any Discord thread under a door channel, including one you create in the Discord UI, starts its own pmjc thread on the first message. Deleting the Discord thread closes the pmjc thread.
- **Slash commands** (guild commands, registered when the bot connects; owner only, everyone else gets an ephemeral refusal):

  | Command | Does |
  |---|---|
  | `/tool name args` | Runs a tool directly with the door's scopes, no model and no cost. Act tools still post Approve/Deny buttons. `args` is a JSON object. |
  | `/fwog new title` | New thread in the door channel, bound to a new pmjc thread |
  | `/fwog close` / `delete` | Archive and lock this thread / delete it after a Confirm button; the pmjc thread is closed |
  | `/fwog trace on\|off` | One `-# tool name(args) -> status` line per tool call in this thread (never results). Default: `discord_door.trace` |
  | `/fwog model provider` / `private on\|off` | Provider (`auto` or a routing id) and local-only for this thread |
  | `/fwog autoapprove add\|remove\|list` | Edit `auto_approve`: those act tools then run without a button, for every caller |
  | `/fwog jobs list\|run\|enable\|disable`, `watches list\|cancel` | Manage jobs and watches |
  | `/fwog notify here` | Send pmjc notifications to this channel, Discord first |
  | `/fwog switch name on\|off` | `monitoring`, `scheduled_jobs`, `auto_start_local` |
  | `/fwog friend add name user_id` / `list` | Remember a Discord user as a contact |
  | `/fwog status` | Door, providers, pending approvals |
- **Friends.** The model knows it can message someone with `discord_dm` (sent as the bot, only to people who share a server with it) or `pmjc_handoff` (a one-tap link so you send it as yourself). Both are act tools, so each needs your approval.
- **Who it answers.** Only the owner ids you set. pmjc never reads anyone else's messages, even in the same channel.
- **Approvals.** Act tools need approval, as they do for every remote caller. The bot posts Approve/Deny buttons in the conversation, and only an owner's click counts. While the door is connected, other approval notifications sent to Discord use buttons too, so they need no public URL.
- **Models.** Requests go through the normal routing: local first, then `claude -p`, then paid APIs if you enabled them. The reply footer names the model that answered.
- **Status:** `pmjc discord-door status` shows the settings and anything missing. Whether the bot is connected: `GET /api/discord-door` with an admin token, or the service log.

Trust: anyone who controls your Discord account can use pmjc with the scopes you gave the door (default `sources,jobs,actions`; actions still need a button click). Turn on Discord two-factor sign-in, or narrow the door with `--scopes sources,jobs`.
