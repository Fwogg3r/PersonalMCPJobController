# Remote access: phone, Claude apps, other tools

pmjc listens on `127.0.0.1` only. To reach it from elsewhere, publish it through a tunnel you control and choose how clients authenticate.

| Client | Auth | Endpoint |
|---|---|---|
| Claude iOS/Android/web/desktop (custom connector) | OAuth (Google or GitHub), identity allowlist | `https://<host>/mcp` |
| iOS Shortcuts, other dashboards, scripts | pmjc token (`Authorization: Bearer`) | `POST https://<host>/api/ask`, `/api/tools/<name>`, `/api/watches/<id>` |
| Other agent CLIs with remote MCP (Codex, Gemini CLI, Cursor, ...) | pmjc token header | `https://<host>/mcp` |

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
