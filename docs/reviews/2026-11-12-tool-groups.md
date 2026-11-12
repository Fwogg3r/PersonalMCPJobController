# Tool groups and the `auto` level (2026-11-12)

Owner request: low-risk tools (Spotify, shop searches, ...) should run without approval at any time, even after a run
read external content. Group tools (music, shopping, social, ...) with group defaults that a tool may override.

## Problem found
Every Spotify read (`spotify_now_playing`, `spotify_devices`, `spotify_queue_list`) returns third-party text, so it
taints the run. After that every act asked for approval and every egress tool (`spotify_queue_search`, `ebay_search`,
`craigslist_search`, `weather_forecast`) was refused outright, so "what's playing? skip it and queue X" never worked.

## Proposal (coordinator)
1. New level `auto`: no approval even after taint; lifts the egress-after-taint refusal; skips the repeat re-ask.
2. Every tool in one group (connector default, tool may declare its own); shipped group defaults.
3. Playlist writes in `music` override to `ask`.
4. Owner `group_levels`; precedence hard floor > tool_levels > auto_approve > group_levels > shipped tool > shipped group.

## Review (Opus-level plan subagent): go ahead with four changes
- **Egress lift needs a data-origin check, not a plain `not sensitive`.** Every Spotify read is private output, so a
  bool would block the owner's main case. Track which groups supplied private data (`sensitive_from`); `auto` egress is
  free when that data comes from the tool's own group (Spotify data back to Spotify isn't a leak), and asks otherwise
  (Gmail data to eBay). Fall back to an approval, not a refusal.
- **Mark fixed-host egress** (`fixed_host`): only such tools may be `auto`, so `*: auto` can't open web_fetch or the
  browser. Cap automatic egress after taint per run.
- **web_search stays gated**: its backend forwards queries to general engines.
- **Precedence was wrong**: `music: auto` would have opened public playlist writes. A shipped per-tool level stricter
  than the group default must survive a looser owner group setting; only `tool_levels` can loosen it. `auto_approve`
  must mean "at least auto_clean", never a downgrade from `auto`.
- Also: `group_levels` into the chat hard-deny list (critical), group `off` must hide tools, `/api/tools` should show
  levels for egress reads and the group, grants must not reject `auto` tools, keep recording acts, audit the new flags.

## Decision
Adopted all of the review, with one adjustment: data from any group whose level is `auto` (music, shopping,
local_info) and the owner's standing context (`pmjc_context`, memory, contacts) also count as harmless sources, so an
eBay search after "what's playing" doesn't ask. Data from mail, files, chat, home, system tools etc. still makes an
`auto` egress call ask. Cap: 30 automatic egress calls per run after taint.

Residual risk accepted: thread history and `model.md` (private, owner's own) may reach a fixed-host search API after
taint; those hosts' logs aren't readable by a third party, and the queries are bounded by the cap.
