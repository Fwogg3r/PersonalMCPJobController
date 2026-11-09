# OpenAI-compatible front door

One chat app for everything: any client that speaks the OpenAI chat API (desktop and mobile apps, Open WebUI, editor plugins) can talk to the pmjc assistant. pmjc picks the model and tools itself, with its normal routing, threads and approvals.

## Connect a client

- Base URL: `https://<your pmjc host>/v1` (or `http://127.0.0.1:8770/v1` on the same machine). The host must be reachable and listed in `allowed_hosts`/`public_url` (see [REMOTE.md](REMOTE.md)).
- API key: a pmjc token (`pmjc token create`). It needs the `sources` scope; add `jobs` and `actions` if the assistant should start watches and propose actions.
- Model: pick one from the client's model list (below).

## Models

`GET /v1/models` lists virtual models:

| id | Behavior |
|---|---|
| `pmjc` | The router's normal provider order |
| `pmjc-local` | Private: local providers only, never falls back to the cloud |
| `pmjc:<tier>` | Uses that tier's providers (`default`, `strong`, `local`, plus your own `router.tiers`); the tier wins over a provider with the same id |
| `pmjc:<provider id>` | Forces that provider first (the rest stay as fallbacks); one entry per enabled provider |

## Conversations and threads

Each chat maps to a pmjc thread, so history, notes and approvals carry over (and show in the dashboard):

1. A client-provided conversation id: header `X-Conversation-Id`, or `conversation_id`, `metadata.conversation_id`, `metadata.chat_id` or `user` in the request body.
2. Otherwise the first user message together with the first assistant reply. A chat with no reply yet is a new conversation and gets a new thread, so two chats that both open with "hi" stay separate.

Either way the key is scoped to the token. Only the **last user message** is used as the request; earlier messages sent by the client are ignored, and the model gets the thread's own history (`front_door.history_turns`). The thread id is returned in `pmjc_thread_id` of non-streaming replies.

## Routing (`router` in settings.yaml)

Each request is routed in this order: an explicit choice (model id, or a rule with a `prefix` such as `/strong`, which is stripped from the request) beats the thread's sticky provider (chosen on its first routed turn), which beats the first matching other rule, which beats the `default` tier. Routing only orders providers; private, capabilities, budget, cooldown and health still filter, so it never widens the candidate set (a private thread asking for `/strong` stays local). Interactive requests (Discord, REST, CLI, `/v1`) never wait for a local host to start: it is started in the background and the next provider answers now.

| key | default | |
|---|---|---|
| `tiers` | built in | `{name: [provider ids]}`, order = fallback. Built in: `default` (routing order), `strong` (non-local), `local` |
| `route_rules` | `/strong`, `/local` prefixes | ordered; first match wins. Matchers: `prefix`, `keywords` (whole words), `surface` (discord, rest, cli, v1, job), `min_chars`, `has_image`; each gives a `tier` |
| `sticky` | true | a thread keeps the provider that answered. After a fallback it remembers the planned provider too: next turn it stays on the fallback if the thread is hard (strong tier, `stay_tool_calls` tool calls, condensed, or history over `stay_history_share` of the planned context), else switches back. The footer says which and offers a pivot (`/local`, `/strong`) |
| `stay_tool_calls`, `stay_history_share` | 4, 0.5 | the "hard thread" thresholds above |
| `gpu_busy_action` | ask | GPU busy and the plan wanted a guarded local provider (`extra.skip_when_gpu_busy_percent`): `ask` offers use local anyway (approval) / use a cloud model / wait; `cloud`; `local`. Private work is never sent to the cloud |
| `gpu_busy_jobs` | wait | the same for jobs and watches: `wait` (a job run is `deferred`, a watch retries after `gpu_busy_backoff_seconds`, up to `gpu_busy_max_waits` times), `cloud` or `local` |
| `footer` | true | last line of an answer, e.g. `⟡ local-27b · rule: /strong · tools: web_search · $0.000`; stripped from history |
| `footer_surfaces` | discord, v1 | REST and CLI JSON answers carry no footer |
| `gpu_busy_processes` | none | process names (games, launchers) that mean the GPU is busy. Otherwise high utilization counts as busy only while pmjc has no local request in flight |

Every routed request is logged (rule, tier, candidates, provider, fallbacks, turns, tool errors, cost); `pmjc routing --days N` summarises it and the dashboard's Routing tab shows it. A `/strong`-style prefix on a thread's next turn is recorded as `retry_of` the previous routed turn (a misroute label).

## Streaming

`stream: true` returns Server-Sent Events in OpenAI's chunk format ending with `data: [DONE]`. pmjc sends the role chunk, then (if `front_door.stream_progress` is on) short progress lines such as `_calling web_search..._`, then the whole answer once it is ready. A comment line (`: keep-alive`) goes out every 10 seconds while waiting so mobile clients don't time out.

## Approvals

Actions with side effects still need your approval. If a request leaves approvals pending, the answer ends with a block listing each action with signed **Approve** and **Deny** links. Once you decide, pmjc continues the thread and stores the outcome; your next message in the same chat includes it in the history. Links are prefixed with `public_url` when it is set; set it when you use a phone app, because many apps can't open relative links.

## Chat commands

A message that starts with one of these is answered by pmjc itself: no model run, instant, free. Same token and scopes as the rest of `/v1`; replies are compact markdown (lists cap at about 60 lines), in both streaming and plain shape.

| Command | Shows |
| --- | --- |
| `/help` | this list |
| `/tools [filter]` | tools your token can use, by connector, with `read` or `act` (needs approval) and a one-line description; the filter matches connector or tool name and also shows tools you lack scope for, marked |
| `/connectors` | each connector: setup, health (3 s timeout, in parallel), usable tool count |
| `/approvals` | your pending approvals with signed Approve / Deny links (admin tokens see all) |
| `/status` | version, uptime, provider order and health, GPU busy, this thread's sticky choice, spend today |
| `/model [tier\|provider\|auto]` | no argument: this thread's routing and the options; with one: sets the thread's sticky choice (`auto` clears it). Never widens: a private thread stays local |
| `/threads` | recent threads |
| `/new` | a fresh thread for this conversation; the next message starts clean |
| `/settings [path]` | the pmjc settings chat may change with current values and allowed direction, providers (id, trust, enabled), your profiles and which is active |
| `/set <path> <value>` or `/set a=1 b=2` | change settings (values are YAML: numbers, `true`/`false`, lists). Same checks as the `pmjc_settings_set` tool: hard-denied paths, safe direction only, bounds, at most 3 paths of one risk class. `off-lower` changes (turning things off, lowering limits) apply at once; `routing` and `cost` changes reply with the old to new diff and signed Approve / Deny links and change nothing until you approve |
| `/profile [name]` | no argument: your profiles (written in `settings.yaml`); with a name: replies with the resulting diff and Approve / Deny links |
| `/revert [backup_id]` | no argument: recent settings backups; with one: the diff against the current file (allowlisted paths only) with Approve / Deny links |

The settings commands need a token with the `actions` scope (or admin) and need no model, so they work when no local model is running. You typed them yourself on an authenticated token, so they are not model proposals: an applied `/set` takes the same file lock, compare-and-swap and backup as the tool, and its audit rows say `owner command`. See "Settings from chat" in [ARCHITECTURE.md](ARCHITECTURE.md). Not wired into the Discord door's slash commands yet.

`/strong` and `/local` are routing prefixes, not commands, and an unknown `/something` goes to the model as normal text. Commands are kept in the thread as `command` items that the model's history leaves out. Turn them off with `front_door.chat_commands: false`.

## Settings (`front_door` in settings.yaml)

| key | default | |
|---|---|---|
| `enabled` | on | `false` turns `/v1` off |
| `stream_progress` | true | progress lines before the answer |
| `history_turns` | 6 | earlier exchanges from the thread given to the model |
| `max_body_bytes` | 1000000 | larger requests get 413 |
| `rate_limit_per_minute` | 30 | per token; over the limit gets 429 |
| `keepalive_seconds` | 10 | SSE keep-alive interval |

`usage` token counts are zeros for now.

## Security

A pmjc token is full owner access for its scopes: whoever holds it, in any chat app, can ask pmjc to read your connected services and propose actions as you. Keep it in the app's secure storage, give it only the scopes you need, revoke it with `pmjc token revoke` if a device is lost, and only expose pmjc through an authenticated tunnel. Approvals still gate every side-effecting action, and links in an answer are signed and expire.
