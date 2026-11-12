---
title: "Everyday connectors: GitHub, Spotify, notes/tasks via mounted MCP servers; deeper Gmail/Calendar tasks"
status: active
links: [phase4/b]
---
**What:** The connectors people use most: GitHub, Spotify and a notes/tasks app through mounted MCP servers (phase4/b), with documented example config and suggested per-tool scopes (reads as read, writes as act); deeper Gmail/Calendar tasks in the existing Google connector (search and summarise mail, draft replies as approved acts, free/busy, create events with approval).

Owner's own server choices and tokens go in the private repo; the public docs give generic examples.

Progress (2026-11-09): notes via a mounted filesystem MCP server (Obsidian vault) documented with a generic example in docs/CONNECTORS.md (pinned version, read tools marked non-egress, writes need approval); deployed for the owner. GitHub and Spotify still to mount.

Progress (2026-11-10): Spotify built as a **native** connector instead of a mount (owner's choice; DECISIONS.md "Spotify connector", review in docs/reviews/2026-11-10-spotify.md). It uses OAuth PKCE on the owner's own developer app and the post-February-2026 Development Mode endpoints. It has 27 tools:
- playback (`auto_clean`; the new `repeatable` tool flag means a second skip in a thread doesn't ask again);
- queue and play, split into by-URI and by-name (egress, one clean call);
- search, playlists, library, history and top items;
- library and playlist writes that always ask, with public playlists behind an option and a `max_volume` cap.

10 mocked tests are in tests/test_spotify.py; the full suite has 546 passing. Not yet tried against a real Spotify account. GitHub is still to do.
