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
| `pmjc:<provider id>` | Forces that provider first (the rest stay as fallbacks); one entry per enabled provider |

## Conversations and threads

Each chat maps to a pmjc thread, so history, notes and approvals carry over (and show in the dashboard):

1. A client-provided conversation id: header `X-Conversation-Id`, or `conversation_id`, `metadata.conversation_id`, `metadata.chat_id` or `user` in the request body.
2. Otherwise the first user message together with the first assistant reply. A chat with no reply yet is a new conversation and gets a new thread, so two chats that both open with "hi" stay separate.

Either way the key is scoped to the token. Only the **last user message** is used as the request; earlier messages sent by the client are ignored, and the model gets the thread's own history (`front_door.history_turns`). The thread id is returned in `pmjc_thread_id` of non-streaming replies.

## Streaming

`stream: true` returns Server-Sent Events in OpenAI's chunk format ending with `data: [DONE]`. pmjc sends the role chunk, then (if `front_door.stream_progress` is on) short progress lines such as `_calling web_search..._`, then the whole answer once it is ready. A comment line (`: keep-alive`) goes out every 10 seconds while waiting so mobile clients don't time out.

## Approvals

Actions with side effects still need your approval. If a request leaves approvals pending, the answer ends with a block listing each action with signed **Approve** and **Deny** links. Once you decide, pmjc continues the thread and stores the outcome; your next message in the same chat includes it in the history. Links are prefixed with `public_url` when it is set; set it when you use a phone app, because many apps can't open relative links.

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
