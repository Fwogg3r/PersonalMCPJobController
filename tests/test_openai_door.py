"""OpenAI-compatible front door (/v1): models, completions, streaming, thread mapping, approvals. ask() is faked."""
from __future__ import annotations

import json
import time

import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME with the fake connectors)


@pytest.fixture
def api(home, monkeypatch):
    from fastapi.testclient import TestClient
    from pmjc import ask as ask_mod, auth, memory, openai_door, server
    openai_door._hits.clear()
    calls = []

    async def fake_ask(caller, text, private=False, provider_order=None, thread_id=None, history_turns=0, extra_system=""):
        calls.append({"text": text, "private": private, "order": provider_order, "thread_id": thread_id,
                      "caller": caller})
        tid = thread_id or memory.thread_new(text, by=caller.name)
        if caller.on_tool:
            await caller.on_tool("web_search", {}, "ok")
        return {"answer": f"echo: {text}", "provider": "p", "watches": [], "thread_id": tid, "cost_usd": 0, "fell_back": []}
    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    h = {"Authorization": "Bearer " + auth.create_token("phone", ["sources", "jobs", "actions"])}
    return c, h, calls


def body(text="hi", **kw):
    return {"model": "pmjc", "messages": [{"role": "user", "content": text}], **kw}


def test_models_list(api):
    c, h, _ = api
    r = c.get("/v1/models", headers=h).json()
    ids = [m["id"] for m in r["data"]]
    assert r["object"] == "list" and ids[:2] == ["pmjc", "pmjc-local"]
    assert any(i.startswith("pmjc:") for i in ids)


def test_unauthenticated_is_401(api):
    c, _, _ = api
    assert c.get("/v1/models").status_code == 401
    assert c.post("/v1/chat/completions", json=body()).status_code == 401


def test_completion_shape(api):
    c, h, _ = api
    r = c.post("/v1/chat/completions", json=body("hello"), headers=h).json()
    assert r["object"] == "chat.completion" and r["choices"][0]["message"] == {"role": "assistant", "content": "echo: hello"}
    assert r["choices"][0]["finish_reason"] == "stop"
    assert r["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_stream_format(api):
    c, h, _ = api
    r = c.post("/v1/chat/completions", json=body("hello", stream=True), headers=h)
    assert r.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    assert all(ch["object"] == "chat.completion.chunk" for ch in chunks)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks)
    assert "_calling web_search..._" in text and text.endswith("echo: hello")
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_progress_can_be_off(api):
    from pmjc import config
    c, h, _ = api
    s = config.load()
    s.front_door.stream_progress = False
    config.save(s)
    r = c.post("/v1/chat/completions", json=body("hello", stream=True), headers=h)
    assert "_calling" not in r.text and "echo: hello" in r.text


def test_same_conversation_same_thread(api):
    c, h, calls = api
    hh = {**h, "X-Conversation-Id": "abc"}
    t1 = c.post("/v1/chat/completions", json=body("one"), headers=hh).json()["pmjc_thread_id"]
    t2 = c.post("/v1/chat/completions", json=body("two"), headers=hh).json()["pmjc_thread_id"]
    t3 = c.post("/v1/chat/completions", json=body("one", user="other"), headers=h).json()["pmjc_thread_id"]
    assert t1 == t2 and t3 != t1 and calls[1]["thread_id"] == t1
    # no id: hash of the first user message
    m1 = {"model": "pmjc", "messages": [{"role": "user", "content": "start"}]}
    a = c.post("/v1/chat/completions", json=m1, headers=h).json()["pmjc_thread_id"]
    m2 = {"model": "pmjc", "messages": [{"role": "user", "content": "start"}, {"role": "assistant", "content": "x"},
                                        {"role": "user", "content": "more"}]}
    b = c.post("/v1/chat/completions", json=m2, headers=h).json()["pmjc_thread_id"]
    assert a == b
    assert calls[-1]["text"] == "more"


def test_local_and_provider_models(api):
    from pmjc import config
    c, h, calls = api
    c.post("/v1/chat/completions", json={**body(), "model": "pmjc-local"}, headers=h)
    assert calls[-1]["private"] is True
    enabled = [p.id for p in config.load().routing if p.enabled]
    pid = enabled[-1]
    c.post("/v1/chat/completions", json={**body("x"), "model": f"pmjc:{pid}"}, headers=h)
    assert calls[-1]["order"][0] == pid and calls[-1]["private"] is False
    c.post("/v1/chat/completions", json=body("y"), headers=h)
    assert calls[-1]["order"] is None
    assert c.post("/v1/chat/completions", json={**body(), "model": "gpt-4"}, headers=h).status_code == 404


def test_bad_requests(api):
    c, h, _ = api
    assert c.post("/v1/chat/completions", json={"model": "pmjc", "messages": []}, headers=h).status_code == 400
    assert c.post("/v1/chat/completions", content=b"x" * 2_000_000, headers=h).status_code == 413


def test_pending_approval_block(api, monkeypatch):
    from pmjc import ask as ask_mod, db, memory
    c, h, _ = api

    async def ask_with_pending(caller, text, **kw):
        tid = kw.get("thread_id") or memory.thread_new(text)
        db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires,thread_id) "
             "VALUES('ap1','fake_send','{}','h','t','send it','pending',?,?,?)", time.time(), time.time() + 60, tid)
        return {"answer": "Needs approval.", "thread_id": tid}
    monkeypatch.setattr(ask_mod, "ask", ask_with_pending)
    out = c.post("/v1/chat/completions", json=body("send"), headers=h).json()["choices"][0]["message"]["content"]
    assert out.startswith("Needs approval.") and "fake_send" in out
    assert "/approve/ap1?sig=" in out and "deny=1" in out
