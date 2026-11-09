"""Chat commands on /v1 (/help, /tools, /connectors, /approvals, /status, /model, /threads, /new). ask() is faked, so
`calls` shows whether a command reached the model."""
from __future__ import annotations

import json
import time

import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME with the fake connectors)
from test_openai_door import api, body  # noqa: F401


def providers():
    from pmjc import config
    s = config.load()
    s.routing = [config.ProviderConfig(id="loc", kind="openai_compat", trust="local", base_url="http://127.0.0.1:1/v1"),
                 config.ProviderConfig(id="cld", kind="openai_compat", trust="cloud", base_url="http://127.0.0.1:2/v1")]
    s.router.sticky = True
    config.save(s)


def say(c, h, text, stream=False, **kw):
    r = c.post("/v1/chat/completions", json=body(text, stream=stream, **kw), headers=h)
    if not stream:
        return r.json()["choices"][0]["message"]["content"]
    lines = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    return "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)


@pytest.mark.parametrize("stream", [False, True])
def test_commands_stream_and_plain_no_model_run(api, stream):
    c, h, calls = api
    providers()
    for cmd, needle in [("/help", "/tools"), ("/tools", "fake_read"), ("/connectors", "fake"), ("/approvals", "approval"),
                        ("/status", "pmjc 0."), ("/model", "Tiers"), ("/threads", "thread"), ("/new", "New thread")]:
        assert needle in say(c, h, cmd, stream=stream), cmd
    assert calls == []


def test_commands_case_insensitive_and_unknown_goes_to_model(api):
    c, h, calls = api
    assert "pmjc chat commands" in say(c, h, "/HELP")
    assert say(c, h, "/foo bar") == "echo: /foo bar"
    assert calls[-1]["text"] == "/foo bar"


def test_commands_can_be_turned_off(api):
    from pmjc import config
    c, h, _ = api
    s = config.load()
    s.front_door.chat_commands = False
    config.save(s)
    assert say(c, h, "/help") == "echo: /help"


def test_tools_filter_and_scope_marks(api):
    from pmjc import auth
    c, h, _ = api
    out = say(c, h, "/tools")
    assert "`fake_read` (read)" in out and "`fake_send` (act)" in out
    only = say(c, h, "/tools send")
    assert "fake_send" in only and "fake_read" not in only
    assert "No tools match" in say(c, h, "/tools zzzz")
    ro = {"Authorization": "Bearer " + auth.create_token("ro", ["sources"])}
    out = say(c, ro, "/tools")
    assert "fake_read" in out and "fake_send" not in out and "lacks" in out
    assert "`fake_send` (act, no access)" in say(c, ro, "/tools fake_send")


def test_tools_cap(api, monkeypatch):
    from pmjc import chat_commands
    c, h, _ = api
    monkeypatch.setattr(chat_commands, "MAX_LINES", 3)
    assert "more, use /tools <filter>" in say(c, h, "/tools")


def test_approvals_only_callers_own_with_signed_links(api):
    from pmjc import auth, db
    c, h, _ = api
    now = time.time()
    for aid, who in (("mine1", "ask:token:phone"), ("other1", "ask:token:someone")):
        db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires) "
             "VALUES(?,?,?,?,?,?,?,?,?)", aid, "fake_send", "{}", "h", who, "send it", "pending", now, now + 60)
    out = say(c, h, "/approvals")
    assert "mine1" in out and "other1" not in out
    assert f"/approve/mine1?sig={auth.sign('approve:mine1')}" in out and "deny=1" in out
    admin = {"Authorization": "Bearer " + auth.create_token("boss", ["admin"])}
    both = say(c, admin, "/approvals")
    assert "mine1" in both and "other1" in both


def test_model_sets_and_clears_sticky(api):
    from pmjc import chat_commands
    c, h, calls = api
    providers()
    hh = {**h, "X-Conversation-Id": "m1"}
    assert "auto" in say(c, hh, "/model")
    assert "`cld`" in say(c, hh, "/model strong")
    tid = c.post("/v1/chat/completions", json=body("/model"), headers=hh).json()["pmjc_thread_id"]
    assert chat_commands.route_row(tid)["provider"] == "cld"
    assert "sticky `cld`" in say(c, hh, "/status")
    assert "Unknown model" in say(c, hh, "/model nope")
    assert "auto" in say(c, hh, "/model auto")
    assert chat_commands.route_row(tid) is None
    assert calls == []


def test_model_never_widens_private_thread(api):
    from pmjc import chat_commands
    c, h, _ = api
    providers()
    hh = {**h, "X-Conversation-Id": "p1"}
    r = c.post("/v1/chat/completions", json={**body("/model strong"), "model": "pmjc-local"}, headers=hh).json()
    assert "private" in r["choices"][0]["message"]["content"]
    assert chat_commands.route_row(r["pmjc_thread_id"]) is None
    r = c.post("/v1/chat/completions", json={**body("/model default"), "model": "pmjc-local"}, headers=hh).json()
    assert chat_commands.route_row(r["pmjc_thread_id"])["provider"] == "loc"


def test_new_maps_conversation_to_new_thread(api):
    c, h, calls = api
    hh = {**h, "X-Conversation-Id": "n1"}
    t1 = c.post("/v1/chat/completions", json=body("hello"), headers=hh).json()["pmjc_thread_id"]
    r = c.post("/v1/chat/completions", json=body("/new"), headers=hh).json()
    t2 = r["pmjc_thread_id"]
    assert t2 != t1 and t2 in r["choices"][0]["message"]["content"]
    c.post("/v1/chat/completions", json=body("again"), headers=hh)
    assert calls[-1]["thread_id"] == t2


def test_new_pair_keyed_conversation(api):
    c, h, calls = api
    r1 = c.post("/v1/chat/completions", json=body("start"), headers=h).json()
    a, reply = r1["pmjc_thread_id"], r1["choices"][0]["message"]["content"]
    first = [{"role": "user", "content": "start"}, {"role": "assistant", "content": reply}]
    r2 = c.post("/v1/chat/completions", json={"model": "pmjc", "messages": first + [{"role": "user", "content": "/new"}]},
                headers=h).json()
    b = r2["pmjc_thread_id"]
    assert b != a
    nxt = first + [{"role": "user", "content": "/new"},
                   {"role": "assistant", "content": r2["choices"][0]["message"]["content"]},
                   {"role": "user", "content": "more"}]
    c.post("/v1/chat/completions", json={"model": "pmjc", "messages": nxt}, headers=h)
    assert calls[-1]["thread_id"] == b


def test_strong_prefix_still_routes_to_model(api):
    c, h, calls = api
    assert say(c, h, "/strong hello") == "echo: /strong hello"
    assert calls[-1]["text"] == "/strong hello"


def test_commands_not_in_model_history(api):
    from pmjc import ask as ask_mod, memory
    c, h, _ = api
    hh = {**h, "X-Conversation-Id": "h1"}
    tid = c.post("/v1/chat/completions", json=body("hello"), headers=hh).json()["pmjc_thread_id"]
    say(c, hh, "/tools")
    say(c, hh, "/help")
    items = memory.thread_get(tid)["items"]
    assert [i["kind"] for i in items].count("command") == 2
    text, _, _ = ask_mod.history(tid, 6)
    assert "/tools" not in text and "/help" not in text
