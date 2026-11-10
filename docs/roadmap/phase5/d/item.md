---
title: "Replies to bot DMs: forward to the owner and add to the sending thread"
status: done
links: [phase5/e]
---
Non-owner DMs to the bot are forwarded to each owner's DM with a header (`DM from <name> (@user, id N)`, plus `re: <thread title>` when routed), with mentions disabled. Generic reply routes: any act tool can return `reply_route: "<service>:<peer>"`; the executor stores which thread sent it (direct and approved runs), and `discord_dm` now does. An incoming reply is added to that thread as an untrusted `reply` item (thread marked updated/unread), so the next run there sees it (and is tainted). Nothing runs on a reply. A reply can span several messages (the route stays until another thread messages that person); only messages that arrive after the send are added, never the earlier DM history. Config `discord_door.forward_dms` (default on); docs in REMOTE.md. Tests: forwarding, routing into the thread, opt-out. Not yet tested live on Discord. Next: the same reply_route for telegram_send/slack_send when their inbound side exists.
