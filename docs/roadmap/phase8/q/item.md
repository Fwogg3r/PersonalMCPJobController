---
title: "Acts on the owner's own data after reading it: add the playing song to a playlist, write a note after listing the vault (design debate first)"
status: idea
links: [phase8/p, phase6/o]
---
**Found by the smoke test (2026-11-11):** reading Spotify (now playing, playlist tracks) or listing the notes vault marks the run untrusted, so the next egress act is refused before approval: 'add this song to my playlist' (spotify_playlist_add is egress because shared playlists are visible to others), 'remove the first song of my playlist', and 'create smoke-new.md in my notes' after a directory listing all fail at every thinking level. The model then tells the user to start a fresh request.

**Options to debate:** (1) treat ids (track URIs, playlist ids, file paths) copied from a tool result as structured data, not text, and allow an egress act whose arguments contain only such ids plus the user's own words; (2) per-tool or per-mount 'output is the owner's own data' (track names are written by artists, so this is weaker for Spotify than for the owner's notes); (3) keep the refusal and offer a one-tap approval that resets the taint for that single act. Tie in with phase8/p (reply after reading).
