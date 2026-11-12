"""Spotify: playback control, search, playlists and library through your own Spotify developer app (OAuth PKCE).

Setup (once): https://developer.spotify.com/dashboard -> Create app -> select "Web API" -> add the redirect URI
http://127.0.0.1:<pmjc port>/oauth/spotify/callback (default port 8770; Spotify refuses "localhost", use the IP).
Copy the Client ID into pmjc (no secret needed: sign-in uses PKCE), then Connect. Development Mode apps need a
Premium account and allow at most five users, and playback control always needs Premium.

Uses the endpoints Spotify kept for Development Mode apps after February 2026 (`/playlists/{id}/items`,
`POST /me/playlists`, `PUT|DELETE /me/library`, search limit 10).

Flags (see DECISIONS.md "Spotify connector"): names of tracks, artists, playlists and devices are third-party text
(untrusted_output). Free-text search sends a model-chosen query off the machine (egress), so queue/play by name are
separate tools from queue/play by URI, which send only ids and stay usable (with approval) after taint.
"""
from __future__ import annotations

import re

from ..connector import Connector, Field, Health, tool
from ..oauth2 import OAuth2Mixin

API = "https://api.spotify.com/v1"
URI = re.compile(r"spotify:(track|episode|album|playlist|artist|show):[A-Za-z0-9]{22}")
PLAYABLE = re.compile(r"spotify:(track|episode):[A-Za-z0-9]{22}")
CONTEXT = re.compile(r"spotify:(album|playlist|artist|show):[A-Za-z0-9]{22}")
ID = re.compile(r"[A-Za-z0-9]{22}")
DEVICE = re.compile(r"[A-Za-z0-9_-]{1,128}")
SEARCH_TYPES = {"track", "album", "artist", "playlist", "show", "episode"}
MAX_ITEMS = 20
MAX_QUERIES = 5


def _check(value: str | None, pattern: re.Pattern, what: str) -> str:
    value = (value or "").strip()
    if not pattern.fullmatch(value):
        raise ValueError(f"{what} must match {pattern.pattern}, got {value[:60]!r}")
    return value


def _queries(queries: list[str]) -> list[str]:
    qs = [q.strip()[:200] for q in (queries or []) if q and q.strip()]
    if not qs or len(qs) > MAX_QUERIES:
        raise ValueError(f"give 1 to {MAX_QUERIES} queries")
    return qs


def _artists(o: dict) -> str:
    return ", ".join(a.get("name", "") for a in o.get("artists") or [])


def _item(o: dict | None) -> dict | None:
    """A compact view of a track, episode, album, artist, playlist or show."""
    if not o:
        return None
    kind = o.get("type")
    d = {"uri": o.get("uri"), "type": kind, "name": o.get("name")}
    if kind == "track":
        d |= {"artists": _artists(o), "album": (o.get("album") or {}).get("name"), "duration_ms": o.get("duration_ms")}
    elif kind == "episode":
        d |= {"show": (o.get("show") or {}).get("name"), "duration_ms": o.get("duration_ms")}
    elif kind == "album":
        d |= {"artists": _artists(o), "release_date": o.get("release_date")}
    elif kind == "playlist":
        d |= {"id": o.get("id"), "owner": (o.get("owner") or {}).get("display_name"), "public": o.get("public")}
    return d


class Spotify(OAuth2Mixin, Connector):
    id = "spotify"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ["spotify", "music", "song", "songs", "track", "playlist", "album", "artist", "queue", "skip", "pause",
               "play", "playing", "volume", "shuffle", "podcast"]
    name = "Spotify"
    description = "Control Spotify playback (play, pause, skip, queue, volume, devices), search, playlists and saved music."
    docs_url = "https://developer.spotify.com/dashboard"
    limitations = ("Needs the owner's Spotify developer app (Client ID) and sign-in. Playback control needs Spotify Premium "
                   "and an active device (a Spotify app that is open; spotify_devices lists them, spotify_transfer wakes "
                   "one). Can't download audio, read lyrics or get recommendations. Track, artist, playlist and device "
                   "names are third-party text: after reading them, playback changes need approval and name searches "
                   "are refused, so put every song in ONE spotify_queue_search (or spotify_search) call, then use URIs.")
    native_alternatives = "a Spotify connector in your client, or the Spotify app itself."
    credentials = [Field("client_id", "Client ID", secret=False,
                         help="From your app at developer.spotify.com/dashboard. Redirect URI: "
                              "http://127.0.0.1:8770/oauth/spotify/callback (use your pmjc port)."),
                   Field("client_secret", "Client secret (optional, PKCE works without it)", required=False),
                   Field("account", "Sign in", kind="oauth2", required=False)]
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    BASE_SCOPES = ["user-read-playback-state", "user-modify-playback-state", "user-read-currently-playing",
                   "user-read-recently-played", "user-top-read", "user-library-read", "user-library-modify",
                   "user-follow-modify", "playlist-read-private", "playlist-read-collaborative", "playlist-modify-private"]

    @property
    def SCOPES(self) -> list[str]:  # noqa: N802 (OAuth2Mixin reads self.SCOPES)
        """Public-playlist writes only when the owner opts in (option public_playlists; sign in again after changing)."""
        return self.BASE_SCOPES + (["playlist-modify-public"] if self.option("public_playlists", False) else [])

    def configured(self) -> bool:
        return super().configured() and self.signed_in()

    async def _api(self, method: str, path: str, params: dict | None = None, json: dict | None = None):
        r = await self.http.request(method, API + path, params={k: v for k, v in (params or {}).items() if v is not None},
                                    json=json, headers={"Authorization": f"Bearer {await self.access_token()}"})
        if r.status_code >= 400:
            msg, reason = r.text[:200], None
            try:
                err = r.json().get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                reason = err.get("reason") if isinstance(err, dict) else None
            except ValueError:
                pass
            hint = {"NO_ACTIVE_DEVICE": " Open Spotify on a device, or pick one with spotify_devices + spotify_transfer.",
                    "PREMIUM_REQUIRED": " Playback control needs Spotify Premium."}.get(reason or "", "")
            if r.status_code == 404 and not hint and path.startswith("/me/player"):
                hint = " No active device? Open Spotify somewhere, or use spotify_devices + spotify_transfer."
            raise RuntimeError(f"Spotify {r.status_code}: {msg or 'error'}{hint}")
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    async def health(self) -> Health:
        if not self.signed_in():
            return Health(False, "not signed in")
        try:
            await self.access_token()
            return Health(True, "signed in")
        except Exception as e:
            return Health(False, type(e).__name__)

    def _device(self, device_id: str | None) -> str | None:
        return _check(device_id, DEVICE, "device_id") if device_id else None

    def _uris(self, uris: list[str], pattern: re.Pattern = PLAYABLE, what: str = "spotify:track/episode URI") -> list[str]:
        u = [_check(x, pattern, what) for x in (uris or [])]
        if not u or len(u) > MAX_ITEMS:
            raise ValueError(f"give 1 to {MAX_ITEMS} URIs")
        return u

    async def _top(self, query: str, type: str = "track") -> dict | None:
        d = await self._api("GET", "/search", {"q": query[:200], "type": type, "limit": 1})
        hits = [x for x in (d.get(type + "s") or {}).get("items", []) if x]
        return _item(hits[0]) if hits else None

    # ---- reading ----

    @tool(scope="read", untrusted_output=True)
    async def spotify_now_playing(self) -> dict:
        """What is playing now: track/episode, device, progress, shuffle and repeat state. {"playing": false} if nothing."""
        p = await self._api("GET", "/me/player", {"additional_types": "track,episode"})
        if not p:
            return {"playing": False, "note": "nothing playing and no active device"}
        dev = p.get("device") or {}
        return {"playing": p.get("is_playing", False), "item": _item(p.get("item")), "progress_ms": p.get("progress_ms"),
                "device": {"id": dev.get("id"), "name": dev.get("name"), "type": dev.get("type"),
                           "volume_percent": dev.get("volume_percent")},
                "shuffle": p.get("shuffle_state"), "repeat": p.get("repeat_state"),
                "context": (p.get("context") or {}).get("uri")}

    @tool(scope="read", untrusted_output=True)  # other people's Connect speakers and Jams show up here
    async def spotify_devices(self) -> list[dict]:
        """The owner's Spotify devices (ids for device_id and spotify_transfer). Only devices with Spotify open appear."""
        d = await self._api("GET", "/me/player/devices")
        return [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type"), "active": x.get("is_active"),
                 "volume_percent": x.get("volume_percent")} for x in d.get("devices", [])]

    @tool(scope="read", untrusted_output=True)
    async def spotify_queue_list(self, limit: int = 10) -> dict:
        """The current track and the next items in the playback queue."""
        d = await self._api("GET", "/me/player/queue")
        return {"current": _item(d.get("currently_playing")),
                "queue": [_item(x) for x in (d.get("queue") or [])[:max(1, min(limit, 50))]]}

    @tool(scope="read", untrusted_output=True, egress=True)
    async def spotify_search(self, queries: list[str], type: str = "track", limit: int = 5) -> list[dict]:
        """Search the Spotify catalog; up to 5 queries in ONE call (after reading results a run can't search again).
        type: track, album, artist, playlist, show or episode. Returns uris for spotify_play / spotify_queue /
        spotify_playlist_add. Spotify caps limit at 10 per query."""
        if type not in SEARCH_TYPES:
            raise ValueError(f"type must be one of {sorted(SEARCH_TYPES)}")
        out = []
        for q in _queries(queries):
            d = await self._api("GET", "/search", {"q": q, "type": type, "limit": max(1, min(limit, 10))})
            out.append({"query": q, "results": [_item(x) for x in (d.get(type + "s") or {}).get("items", []) if x]})
        return out

    @tool(scope="read", untrusted_output=True)
    async def spotify_playlists(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """The owner's playlists (own and followed); `id` is the playlist_id for spotify_playlist_tracks and
        spotify_playlist_add, `uri` the context_uri for spotify_play."""
        d = await self._api("GET", "/me/playlists", {"limit": max(1, min(limit, 50)), "offset": max(0, offset)})
        return [_item(p) for p in d.get("items", []) if p]

    @tool(scope="read", untrusted_output=True)
    async def spotify_playlist_tracks(self, playlist_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Items of a playlist the owner owns or collaborates on (Spotify hides others' items). playlist_id comes
        from spotify_playlists."""
        pid = _check(playlist_id, ID, "playlist_id")
        d = await self._api("GET", f"/playlists/{pid}/items", {"limit": max(1, min(limit, 50)), "offset": max(0, offset)})
        return [_item(x.get("item") or x.get("track")) for x in d.get("items", []) if x]

    @tool(scope="read", untrusted_output=True)
    async def spotify_saved(self, limit: int = 20, offset: int = 0) -> list[dict]:
        """The owner's saved (liked) tracks, newest first."""
        d = await self._api("GET", "/me/tracks", {"limit": max(1, min(limit, 50)), "offset": max(0, offset)})
        return [_item(x["track"]) | {"added_at": x.get("added_at")} for x in d.get("items", []) if x.get("track")]

    @tool(scope="read", untrusted_output=True)
    async def spotify_recent(self, limit: int = 20) -> list[dict]:
        """Recently played tracks, newest first."""
        d = await self._api("GET", "/me/player/recently-played", {"limit": max(1, min(limit, 50))})
        return [_item(x["track"]) | {"played_at": x.get("played_at")} for x in d.get("items", []) if x.get("track")]

    @tool(scope="read", untrusted_output=True)
    async def spotify_top(self, type: str = "tracks", time_range: str = "medium_term", limit: int = 20) -> list[dict]:
        """The owner's top tracks or artists. type: tracks | artists. time_range: short_term (~4 weeks),
        medium_term (~6 months), long_term (~1 year)."""
        if type not in {"tracks", "artists"}:
            raise ValueError("type must be tracks or artists")
        if time_range not in {"short_term", "medium_term", "long_term"}:
            raise ValueError("time_range must be short_term, medium_term or long_term")
        d = await self._api("GET", f"/me/top/{type}", {"time_range": time_range, "limit": max(1, min(limit, 50))})
        return [_item(x) for x in d.get("items", [])]

    # ---- playback: no lasting effect, ids only -> auto_clean, repeatable ----

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_pause(self, device_id: str | None = None) -> dict:
        """Pause playback."""
        await self._api("PUT", "/me/player/pause", {"device_id": self._device(device_id)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_resume(self, device_id: str | None = None) -> dict:
        """Resume playback where it stopped. To start something specific use spotify_play."""
        await self._api("PUT", "/me/player/play", {"device_id": self._device(device_id)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_next(self, device_id: str | None = None) -> dict:
        """Skip to the next track."""
        await self._api("POST", "/me/player/next", {"device_id": self._device(device_id)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_previous(self, device_id: str | None = None) -> dict:
        """Go back to the previous track."""
        await self._api("POST", "/me/player/previous", {"device_id": self._device(device_id)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_seek(self, position_ms: int, device_id: str | None = None) -> dict:
        """Jump to a position in the current track, in milliseconds (past the end skips to the next track)."""
        await self._api("PUT", "/me/player/seek", {"position_ms": max(0, int(position_ms)), "device_id": self._device(device_id)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_volume(self, volume_percent: int, device_id: str | None = None) -> dict:
        """Set the volume, 0-100 (capped at the owner's max_volume option). Some devices (phones, some speakers)
        don't allow remote volume."""
        cap = max(0, min(100, int(self.option("max_volume", 100))))
        v = max(0, min(cap, int(volume_percent)))
        await self._api("PUT", "/me/player/volume", {"volume_percent": v, "device_id": self._device(device_id)})
        return {"ok": True, "volume_percent": v}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_shuffle(self, state: bool, device_id: str | None = None) -> dict:
        """Turn shuffle on or off."""
        await self._api("PUT", "/me/player/shuffle", {"state": "true" if state else "false", "device_id": self._device(device_id)})
        return {"ok": True, "shuffle": bool(state)}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_repeat(self, state: str, device_id: str | None = None) -> dict:
        """Set repeat: track, context (the album/playlist) or off."""
        if state not in {"track", "context", "off"}:
            raise ValueError("state must be track, context or off")
        await self._api("PUT", "/me/player/repeat", {"state": state, "device_id": self._device(device_id)})
        return {"ok": True, "repeat": state}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_transfer(self, device_id: str, play: bool = True) -> dict:
        """Move playback to another device (device_id from spotify_devices); play=true starts it there."""
        await self._api("PUT", "/me/player", json={"device_ids": [_check(device_id, DEVICE, "device_id")], "play": bool(play)})
        return {"ok": True}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_queue(self, uris: list[str], device_id: str | None = None) -> dict:
        """Add spotify:track/episode URIs (from spotify_search, playlists, history) to the play queue, in order (max 20).
        Sends only ids, so it still works (with approval) after the run read track names. To queue by song name,
        use spotify_queue_search."""
        dev = self._device(device_id)
        u = self._uris(uris)
        for x in u:
            await self._api("POST", "/me/player/queue", {"uri": x, "device_id": dev})
        return {"ok": True, "queued": len(u)}

    @tool(scope="act", level="auto_clean", untrusted_output=True, egress=True)
    async def spotify_queue_search(self, queries: list[str], device_id: str | None = None) -> dict:
        """Queue songs by name: each query (e.g. "Bohemian Rhapsody Queen") queues its top track match, in order.
        Put every song in ONE call (max 5): afterwards the run has read track names and can't search again.
        Returns what was queued."""
        qs = _queries(queries)
        dev = self._device(device_id)
        queued, missing = [], []
        for q in qs:
            t = await self._top(q)
            if t and PLAYABLE.fullmatch(t.get("uri") or ""):
                await self._api("POST", "/me/player/queue", {"uri": t["uri"], "device_id": dev})
                queued.append(t)
            else:
                missing.append(q)
        return {"queued": queued, "not_found": missing}

    @tool(scope="act", level="auto_clean", repeatable=True)
    async def spotify_play(self, uris: list[str] | None = None, context_uri: str | None = None, offset: int = 0,
                           device_id: str | None = None) -> dict:
        """Start playing now, replacing what plays: either `uris` (spotify:track/episode URIs, max 20) or
        `context_uri` (a spotify:album/playlist/artist/show URI, starting at item `offset`). Get URIs from
        spotify_search or spotify_playlists. To play by name use spotify_play_search; to add without
        interrupting, spotify_queue."""
        if bool(uris) == bool(context_uri):
            raise ValueError("give exactly one of uris or context_uri")
        body: dict = {}
        if uris:
            body["uris"] = self._uris(uris)
        else:
            body["context_uri"] = _check(context_uri, CONTEXT, "context_uri")
            if offset and not body["context_uri"].startswith("spotify:artist:"):
                body["offset"] = {"position": max(0, int(offset))}
        await self._api("PUT", "/me/player/play", {"device_id": self._device(device_id)}, json=body)
        return {"ok": True}

    @tool(scope="act", level="auto_clean", untrusted_output=True, egress=True)
    async def spotify_play_search(self, query: str, type: str = "track", device_id: str | None = None) -> dict:
        """Play the top match for a name now, replacing what plays. type: track, album, playlist or artist
        (e.g. "Abbey Road" type=album). Returns what started."""
        if type not in {"track", "album", "playlist", "artist"}:
            raise ValueError("type must be track, album, playlist or artist")
        t = await self._top(query, type)
        if not t or not URI.fullmatch(t.get("uri") or ""):
            return {"ok": False, "not_found": query}
        body = {"uris": [t["uri"]]} if type == "track" else {"context_uri": t["uri"]}
        await self._api("PUT", "/me/player/play", {"device_id": self._device(device_id)}, json=body)
        return {"ok": True, "playing": t}

    # ---- library and playlists: lasting changes -> ask ----

    @tool(scope="act")
    async def spotify_save(self, uris: list[str]) -> dict:
        """Save to the owner's library (like a track, save an album/show/episode, follow a playlist or artist).
        spotify: URIs only, max 20."""
        u = self._uris(uris, URI, "spotify: URI")
        await self._api("PUT", "/me/library", {"uris": ",".join(u)})
        return {"ok": True, "saved": len(u)}

    @tool(scope="act")
    async def spotify_unsave(self, uris: list[str]) -> dict:
        """Remove from the owner's library (unlike, unfollow). spotify: URIs only, max 20."""
        u = self._uris(uris, URI, "spotify: URI")
        await self._api("DELETE", "/me/library", {"uris": ",".join(u)})
        return {"ok": True, "removed": len(u)}

    @tool(scope="act", egress=True)  # shared and collaborative playlists are visible to other people
    async def spotify_playlist_add(self, playlist_id: str, uris: list[str], position: int | None = None) -> dict:
        """Add spotify:track/episode URIs (max 20) to a playlist the owner owns or collaborates on. playlist_id comes
        from spotify_playlists; position (0-based) inserts instead of appending."""
        pid = _check(playlist_id, ID, "playlist_id")
        body: dict = {"uris": self._uris(uris)}
        if position is not None:
            body["position"] = max(0, int(position))
        d = await self._api("POST", f"/playlists/{pid}/items", json=body)
        return {"ok": True, "added": len(body["uris"]), "snapshot_id": d.get("snapshot_id")}

    @tool(scope="act", egress=True)
    async def spotify_playlist_remove(self, playlist_id: str, uris: list[str]) -> dict:
        """Remove every occurrence of these spotify:track/episode URIs (max 20) from a playlist the owner owns."""
        pid = _check(playlist_id, ID, "playlist_id")
        u = self._uris(uris)
        d = await self._api("DELETE", f"/playlists/{pid}/items", json={"items": [{"uri": x} for x in u]})
        return {"ok": True, "removed": len(u), "snapshot_id": d.get("snapshot_id")}

    @tool(scope="act", egress=True)
    async def spotify_playlist_create(self, name: str, description: str = "", public: bool = False) -> dict:
        """Create a playlist for the owner (private unless the owner enabled public_playlists). Add songs with
        spotify_playlist_add."""
        if not name.strip():
            raise ValueError("name is required")
        if public and not self.option("public_playlists", False):
            raise PermissionError("public playlists are off (Spotify option public_playlists); nothing was created")
        p = await self._api("POST", "/me/playlists", json={"name": name[:100], "description": description[:300],
                                                            "public": bool(public)})
        return {"ok": True, "id": p.get("id"), "uri": p.get("uri")}
