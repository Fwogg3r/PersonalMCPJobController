"""Spotify connector against a mocked transport; never calls Spotify."""
from __future__ import annotations

import json
import time

import httpx
import pytest

T1 = "spotify:track:" + "a" * 22
T2 = "spotify:track:" + "b" * 22
ALBUM = "spotify:album:" + "c" * 22
PL = "d" * 22


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import config, creds, db, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load(); s.credential_backend = "file"; config.save(s)
    creds.store().set("spotify.client_id", "cid")
    creds.store().set("spotify.token", json.dumps({"access_token": "tok", "refresh_token": "r",
                                                   "expires_at": time.time() + 3600}))
    yield tmp_path
    db._local.c = None


def _track(uri, name="Song", artist="Band"):
    return {"type": "track", "uri": uri, "name": name, "artists": [{"name": artist}], "album": {"name": "LP"},
            "duration_ms": 1000}


def _server(calls, search_hits=None, status=204, err=None):
    def h(req: httpx.Request):
        calls.append(req)
        if err:
            return httpx.Response(err[0], json={"error": {"status": err[0], "message": "nope", "reason": err[1]}})
        if req.url.path == "/v1/search":
            t = req.url.params["type"]
            return httpx.Response(200, json={t + "s": {"items": (search_hits or {}).get(req.url.params["q"], [])}})
        if req.url.path == "/v1/me/player" and req.method == "GET":
            return httpx.Response(200, json={"is_playing": True, "item": _track(T1), "progress_ms": 5,
                                             "device": {"id": "dev1", "name": "Desk", "volume_percent": 40}})
        if req.url.path.endswith("/items") and req.method != "GET":
            return httpx.Response(201, json={"snapshot_id": "s1"})
        return httpx.Response(status)
    return h


def _conn(handler):
    from pmjc import registry
    reg = registry.reload()
    c = reg.tool("spotify_next").fn.__self__
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return reg, c


def agent(thread="t1"):
    from pmjc import auth
    return auth.Caller("agent", "remote", {"sources", "actions", "jobs"}, thread_id=thread)


async def test_flags():
    from pmjc import registry
    reg = registry.reload()
    from pmjc import config, levels
    s = config.load()
    want = {  # name: (scope, untrusted_output, egress, repeatable, effective level)
        "spotify_now_playing": ("read", True, False, False, "auto"),
        "spotify_devices": ("read", True, False, False, "auto"),
        "spotify_search": ("read", True, True, False, "auto"),
        "spotify_next": ("act", False, False, True, "auto"),
        "spotify_volume": ("act", False, False, True, "auto"),
        "spotify_queue": ("act", False, False, True, "auto"),
        "spotify_queue_search": ("act", True, True, False, "auto"),
        "spotify_play_search": ("act", True, True, False, "auto"),
        "spotify_save": ("act", False, False, False, "auto"),
        "spotify_playlist_add": ("act", False, True, False, "ask"),  # shared playlists: overrides the music group
        "spotify_playlist_create": ("act", False, True, False, "ask"),
    }
    for name, flags in want.items():
        t = reg.tool(name)
        assert t.group == "music", name
        assert (t.scope, t.untrusted_output, t.egress, t.repeatable, levels.for_tool(t, s)) == flags, name


async def test_scopes_public_only_when_enabled():
    from pmjc import config
    _, c = _conn(_server([]))
    assert "playlist-modify-public" not in c.SCOPES
    url = c.oauth_start("http://127.0.0.1:8770/oauth/spotify/callback")
    assert url.startswith("https://accounts.spotify.com/authorize?") and "code_challenge=" in url
    s = config.load(); s.connectors["spotify"] = {"public_playlists": True}; config.save(s)
    assert "playlist-modify-public" in c.SCOPES


async def test_skip_runs_without_approval_and_repeats():
    from pmjc import executor
    calls = []
    _conn(_server(calls))
    a = agent()
    for _ in range(3):  # repeatable: the thread's act ledger doesn't re-ask
        assert (await executor.call_tool(a, "spotify_next", {})) == {"ok": True}
    assert [r.url.path for r in calls] == ["/v1/me/player/next"] * 3
    assert calls[0].headers["authorization"] == "Bearer tok"


async def test_music_group_runs_after_external_context():
    from pmjc import executor
    calls = []
    _conn(_server(calls, {"song a": [_track(T1)]}))
    a = agent()
    np = await executor.call_tool(a, "spotify_now_playing", {})
    assert np["item"]["uri"] == T1 and a.untrusted
    assert await executor.call_tool(a, "spotify_next", {}) == {"ok": True}  # group music is `auto`: no approval
    r = await executor.call_tool(a, "spotify_queue_search", {"queries": ["song a"]})  # fixed-host egress after taint
    assert [x["uri"] for x in r["queued"]] == [T1] and a.auto_egress == 1
    with pytest.raises(executor.Denied):  # playlist writes override the group to `ask`: the old egress rule applies
        await executor.call_tool(a, "spotify_playlist_add", {"playlist_id": "p1", "uris": [T2]})


async def test_music_auto_egress_asks_when_run_holds_other_private_data():
    from pmjc import executor
    _conn(_server([]))
    a = agent()
    await executor.call_tool(a, "spotify_now_playing", {})
    a.sensitive_from.add("email_calendar")  # e.g. the run read a mail
    r = await executor.call_tool(a, "spotify_queue_search", {"queries": ["x"]})
    assert r["status"] == "pending_approval"
    assert await executor.call_tool(a, "spotify_next", {}) == {"ok": True}  # no data leaves: still automatic


async def test_owner_group_level_tightens_music():
    from pmjc import config, executor
    s = config.load(); s.group_levels = {"music": "ask"}; config.save(s)
    _conn(_server([]))
    a = agent()
    await executor.call_tool(a, "spotify_now_playing", {})
    assert (await executor.call_tool(a, "spotify_next", {}))["status"] == "pending_approval"
    with pytest.raises(executor.Denied):  # back to the old egress rule
        await executor.call_tool(a, "spotify_queue_search", {"queries": ["x"]})


async def test_queue_search_one_call():
    from pmjc import executor
    calls = []
    _conn(_server(calls, {"song a": [_track(T1)], "song b": [_track(T2, "B")]}))
    r = await executor.call_tool(agent(), "spotify_queue_search", {"queries": ["song a", "missing", "song b"]})
    assert [t["uri"] for t in r["queued"]] == [T1, T2] and r["not_found"] == ["missing"]
    q = [x for x in calls if x.url.path == "/v1/me/player/queue"]
    assert [x.url.params["uri"] for x in q] == [T1, T2]


async def test_uri_validation():
    _, c = _conn(_server([]))
    for bad in (["spotify:track:short"], ["https://evil.example/x"], [ALBUM], [T1] * 21, []):
        with pytest.raises(ValueError):
            await c.spotify_queue(bad)
    with pytest.raises(ValueError):
        await c.spotify_play(uris=[T1], context_uri=ALBUM)
    with pytest.raises(ValueError):
        await c.spotify_playlist_add("../me", [T1])
    with pytest.raises(ValueError):
        await c.spotify_search([str(i) for i in range(6)])


async def test_play_context_and_volume_cap():
    from pmjc import config
    calls = []
    _, c = _conn(_server(calls))
    await c.spotify_play(context_uri=ALBUM, offset=2, device_id="dev1")
    r = calls[-1]
    assert r.method == "PUT" and r.url.params["device_id"] == "dev1"
    assert json.loads(r.content) == {"context_uri": ALBUM, "offset": {"position": 2}}
    s = config.load(); s.connectors["spotify"] = {"max_volume": 60}; config.save(s)
    assert (await c.spotify_volume(95))["volume_percent"] == 60
    assert calls[-1].url.params["volume_percent"] == "60"


async def test_library_and_playlists_use_2026_endpoints():
    calls = []
    _, c = _conn(_server(calls))
    await c.spotify_save([T1, ALBUM])
    assert calls[-1].method == "PUT" and calls[-1].url.path == "/v1/me/library"
    assert calls[-1].url.params["uris"] == f"{T1},{ALBUM}"
    r = await c.spotify_playlist_add(PL, [T1, T2], position=0)
    assert r["snapshot_id"] == "s1" and calls[-1].url.path == f"/v1/playlists/{PL}/items"
    assert json.loads(calls[-1].content) == {"uris": [T1, T2], "position": 0}
    await c.spotify_playlist_remove(PL, [T1])
    assert json.loads(calls[-1].content) == {"items": [{"uri": T1}]}


async def test_public_playlist_refused_by_default():
    calls = []
    _, c = _conn(_server(calls))
    with pytest.raises(PermissionError):
        await c.spotify_playlist_create("Mix", public=True)
    assert not calls


async def test_errors_are_readable():
    _, c = _conn(_server([], err=(404, "NO_ACTIVE_DEVICE")))
    with pytest.raises(RuntimeError, match="spotify_devices"):
        await c.spotify_next()
    _, c = _conn(_server([], err=(403, "PREMIUM_REQUIRED")))
    with pytest.raises(RuntimeError, match="Premium"):
        await c.spotify_pause()
