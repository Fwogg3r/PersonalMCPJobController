---
title: "Relay threads: bot DMs mirrored into a server channel, one thread per conversation"
status: planned
links: []
---
Replaces the DM forwarding from phase4/m (owner DM copies) with threads in a configured relay channel (e.g. #<bot>-external-chats; config `discord_door.relay_channel_id`, never committed).

Behaviour:
- Outgoing: every discord_dm from pmjc posts its text into the relay thread for that user. If no open relay thread exists, create one titled algorithmically (no model): "<user display name>: <first ~60 chars of the first message>". The thread's first post is that message's content.
- Incoming: a DM from a user goes into their open relay thread. If none is open, a new thread is started the same way from their message. Mentions are always off.
- Owner replies: a message the owner writes in a relay thread is sent to that user as the bot. These threads are not door conversations, so no model runs on them.
- Closing: once a relay thread is closed, nothing more is appended to it, and the next message either way starts a new thread. The owner can also close one on request (/pmjc relay close, or ask pmjc). Open question: Discord auto-archives inactive threads (max 7 days), so "closed" should mean locked or archived by the owner. Proposal: create threads with a 7-day auto-archive, treat THREAD_UPDATE locked=true as closed, and give the owner a command to close; auto-archived threads reopen when a new message is posted.
- AI access: no model reads relay threads unless asked. A read tool (relay_read(user or contact, since="2d"/max duration)) returns that conversation's messages as untrusted content, so a run that uses it is tainted.
- Storage: table relay(user_id, thread_id, opened, closed). Replies to discord_dm keep using reply_route (phase4/m) so the pmjc conversation that sent the DM still sees them.

Size: roughly 200-300 lines in discord_door.py, discord_commands.py, chat.py and config, plus fake-REST tests (gateway THREAD_UPDATE handling, the owner-in-relay-thread path, auto-start). Deferred on 2026-11-08: about 4% of the weekly plan and $3.52 of API credit left; too big to finish safely in one go.
