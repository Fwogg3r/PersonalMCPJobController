"""Discord member lookup, DM by name and contact saving, against a mocked transport; never calls Discord."""
from __future__ import annotations

import httpx
import pytest

GUILDS = [{"id": "100", "name": "Buddies"}, {"id": "200", "name": "Games"}]
MEMBERS = {
    "100": [{"user": {"id": "11", "username": "emme_x", "global_name": "Emme"}, "nick": None},
            {"user": {"id": "12", "username": "anticat", "global_name": "Anti"}, "nick": "Maceo"},
            {"user": {"id": "99", "username": "somebot", "global_name": "Emme Bot", "bot": True}, "nick": None}],
    "200": [{"user": {"id": "11", "username": "emme_x", "global_name": "Emme"}, "nick": "Em"},
            {"user": {"id": "13", "username": "sam1", "global_name": "Sam"}, "nick": None},
            {"user": {"id": "14", "username": "sam2", "global_name": "Sam"}, "nick": None}],
}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import config, creds, db, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load(); s.credential_backend = "file"; config.save(s)
    creds.store().set("discord.bot_token", "tok")
    yield tmp_path
    db._local.c = None


def _server(calls, list_status=200):
    def h(req: httpx.Request):
        calls.append(req)
        p = req.url.path
        if p.endswith("/users/@me/guilds"):
            return httpx.Response(200, json=GUILDS)
        if "/members" in p:
            gid = p.split("/guilds/")[1].split("/")[0]
            if p.endswith("/members/search"):
                q = req.url.params["query"].lower()
                return httpx.Response(200, json=[m for m in MEMBERS[gid] if m["user"]["username"].startswith(q)
                                                 or (m["nick"] or "").lower().startswith(q)])
            return httpx.Response(list_status, json=MEMBERS[gid] if list_status == 200 else {"message": "Missing Access"})
        if p.endswith("/users/@me/channels"):
            import json
            return httpx.Response(200, json={"id": "dm-" + json.loads(req.content)["recipient_id"]})
        if p.endswith("/messages"):
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(404)
    return h


def _conn(handler):
    from pmjc import registry
    c = registry.reload().tool("discord_dm").fn.__self__
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _sent_to(calls):
    return [r.url.path for r in calls if r.url.path.endswith("/messages") and r.method == "POST"]


async def test_members_lookup_dedupes_and_skips_bots():
    c = _conn(_server([]))
    r = await c.discord_members("emme")
    assert [p["user_id"] for p in r] == ["11"]  # exact display name; the bot is left out
    assert r[0]["servers"] == ["Buddies", "Games"] and r[0]["nicks"] == ["Em"]
    assert [p["user_id"] for p in await c.discord_members("maceo")] == ["12"]  # server nickname
    assert len(await c.discord_members()) == 4


async def test_dm_by_name_finds_member_and_saves_contact():
    from pmjc import memory
    calls = []
    c = _conn(_server(calls))
    r = await c.discord_dm("hi", contact="Emme")  # run after the owner approved it: no caller
    assert r["user_id"] == "11" and r["to"] == "Emme (@emme_x)" and r["contact"] == "active"
    assert _sent_to(calls) == ["/api/v10/channels/dm-11/messages"]
    saved = memory.contact_get("Emme")
    assert saved["handles"]["discord_user_id"] == "11" and saved["handles"]["discord_username"] == "emme_x"
    calls.clear()
    r = await c.discord_dm("again", contact="emme")  # now resolved from the contact: no server lookup
    assert r["user_id"] == "11" and "to" not in r and not [x for x in calls if "/members" in x.url.path]


async def test_dm_refuses_to_guess_between_two_people():
    calls = []
    c = _conn(_server(calls))
    with pytest.raises(ValueError, match="several people match 'Sam'.*sam1.*sam2"):
        await c.discord_dm("hi", contact="Sam")
    assert _sent_to(calls) == []
    with pytest.raises(ValueError, match="nobody"):
        await c.discord_dm("hi", contact="Zed")


async def test_alias_contact_without_id_is_looked_up_by_its_names():
    from pmjc import auth, memory
    memory.contact_set("Maceo", ["Anti"], caller=auth.admin())  # "Anti is Maceo irl", no Discord id yet
    calls = []
    c = _conn(_server(calls))
    r = await c.discord_dm("yo", contact="Anti")
    assert r["user_id"] == "12" and r["contact"] == "active"
    assert memory.contact_resolve("anti")["handles"]["discord_user_id"] == "12"


async def test_falls_back_to_search_and_explains_missing_intent():
    calls = []
    c = _conn(_server(calls, list_status=403))
    r = await c.discord_members("emme_")
    assert [p["user_id"] for p in r] == ["11"]
    with pytest.raises(ValueError, match="Server Members Intent"):
        await c.discord_members()


async def test_contact_from_a_clean_agent_run_follows_contacts_from_chat():
    from pmjc import auth, config, memory
    c = _conn(_server([]))
    agent = auth.Caller("agent", "remote", {"sources", "actions", "jobs"})
    r = await c.discord_dm("hi", contact="Emme", _caller=agent)
    assert r["contact"] == "pending" and memory.contact_get("Emme") == {}
    s = config.load(); s.contacts_from_chat = "direct"; config.save(s)
    r = await c.discord_dm("hi", contact="Emme", _caller=agent)
    assert r["contact"] == "active" and memory.contact_get("Emme")["handles"]["discord_user_id"] == "11"
    tainted = auth.Caller("agent", "remote", {"sources", "actions", "jobs"}, untrusted=True)
    r = await c.discord_dm("hi", contact="Em", _caller=tainted)
    assert r["user_id"] == "11"  # "Em" resolves to the saved contact Emme (partial match), no save needed


async def test_contacts_direct_mode_adds_but_never_redirects():
    from pmjc import auth, config, memory
    s = config.load(); s.contacts_from_chat = "direct"; config.save(s)
    memory.contact_set("Maceo", None, {"discord_user_id": "12"}, caller=auth.admin())
    agent = auth.Caller("agent", "remote", {"sources", "jobs"})
    assert memory.contact_set("Maceo", ["Anti"], caller=agent)["status"] == "active"  # alias on an owner contact
    assert memory.contact_resolve("Anti")["name"] == "Maceo"
    r = memory.contact_set("Maceo", None, {"discord_user_id": "66"}, caller=agent)  # would redirect: pending
    assert r["status"] == "pending" and memory.contact_get("Maceo")["handles"]["discord_user_id"] == "12"
    assert memory.contact_set("Emme", None, {"discord_user_id": "11"}, caller=agent)["status"] == "active"


async def test_discord_members_flags():
    from pmjc import registry
    t = registry.reload().tool("discord_members")
    assert t.scope == "read" and t.untrusted_output and t.group == "social"
