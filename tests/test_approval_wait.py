"""Approvals notify the requesting MCP client: inline wait, pmjc_approval_wait, cross-process polling, no duplicates."""
from __future__ import annotations

import asyncio
import json
import textwrap
import time

import pytest

PLUGIN = '''
from pmjc.connector import Connector, Field, tool


class Fake(Connector):
    id = "fake"
    name = "Fake"
    credentials = [Field("key", "Key")]

    @tool
    async def fake_send(self, to: str) -> dict:
        "Side effect."
        if to == "boom":
            raise RuntimeError("boom")
        return {"sent": to}
'''


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "test-master-key")
    from pmjc import config, creds, db, memory, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    s = config.load()
    s.credential_backend = "file"
    s.approval_poll_seconds = 0.05
    s.approval_wait_seconds = 2
    config.save(s)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "fake.py").write_text(textwrap.dedent(PLUGIN))
    creds.store().set("fake.key", "x")
    yield tmp_path
    db._local.c = None


def caller(name="agent", kind="remote", scopes=("sources", "actions", "jobs")):
    from pmjc import auth
    return auth.Caller(name, kind, set(scopes))


async def request(c, to="a"):
    from pmjc import executor
    res = await executor.call_tool(c, "fake_send", {"to": to})
    assert res["status"] == "pending_approval"
    return res


async def decide_soon(aid, approve=True, delay=0.15):
    from pmjc import executor
    await asyncio.sleep(delay)
    return await executor.decide(aid, approve, "test")


def set_cfg(**kw):
    from pmjc import config
    s = config.load()
    for k, v in kw.items():
        setattr(s, k, v)
    config.save(s)


async def test_inline_wait_returns_the_result_when_decided_during_the_wait():
    from pmjc import mcp_server
    c = caller()
    pend = await request(c)
    assert "pmjc_approval_wait" in pend["note"]
    task = asyncio.create_task(decide_soon(pend["approval_id"]))
    out = await mcp_server.settle_pending(pend)
    await task
    assert out["status"] == "done" and out["result"] == {"sent": "a"}


async def test_inline_wait_returns_denied_and_failed():
    from pmjc import mcp_server
    c = caller()
    pend = await request(c)
    t = asyncio.create_task(decide_soon(pend["approval_id"], approve=False))
    assert (await mcp_server.settle_pending(pend))["status"] == "denied"
    await t
    pend = await request(c, "boom")
    t = asyncio.create_task(decide_soon(pend["approval_id"]))
    out = await mcp_server.settle_pending(pend)
    await t
    assert out["status"] == "failed" and "boom" in out["error"]


async def test_timeout_returns_pending_with_next_instruction():
    from pmjc import mcp_server
    pend = await request(caller())
    out = await mcp_server.settle_pending(pend, wait_seconds=0.2)
    assert out["status"] == "pending_approval" and out["approval_id"] == pend["approval_id"]
    assert "pmjc_approval_wait" in out["next"] and "do not ask the user to prompt you again" in out["next"]


async def test_wrapped_mcp_tool_waits_inline():
    from fastmcp import Client
    from pmjc import db, executor, mcp_server
    mcp = mcp_server.build(stdio=True)

    async def approve_when_pending():
        for _ in range(100):
            rows = db.q("SELECT id FROM approvals WHERE status='pending'")
            if rows:
                return await executor.decide(rows[0]["id"], True, "test")
            await asyncio.sleep(0.02)
    task = asyncio.create_task(approve_when_pending())
    async with Client(mcp) as client:
        res = await client.call_tool("fake_send", {"to": "x"})
    await task
    text = json.dumps(res.structured_content or res.data)
    assert '"done"' in text and '"sent": "x"' in text


async def test_wait_tool_returns_after_decision_and_marks_delivered():
    from pmjc import executor
    c = caller()
    pend = await request(c)
    aid = pend["approval_id"]
    t = asyncio.create_task(decide_soon(aid))
    out = await executor.call_tool(c, "pmjc_approval_wait", {"approval_id": aid, "timeout_seconds": 5})
    await t
    assert out["status"] == "done" and out["result"] == {"sent": "a"}
    assert executor.delivered(aid)


async def test_wait_tool_pending_has_next_and_caps_timeout():
    from pmjc import executor
    c = caller()
    aid = (await request(c))["approval_id"]
    out = await executor.call_tool(c, "pmjc_approval_wait", {"approval_id": aid, "timeout_seconds": 0})
    assert out["status"] == "pending" and "pmjc_approval_wait" in out["next"]
    assert not executor.delivered(aid)


async def test_other_callers_cannot_wait_or_see_but_admin_can():
    from pmjc import executor
    aid = (await request(caller("agent")))["approval_id"]
    other = caller("someone-else", scopes=("sources",))
    with pytest.raises(executor.Denied):
        await executor.call_tool(other, "pmjc_approval_wait", {"approval_id": aid, "timeout_seconds": 0})
    assert (await executor.call_tool(other, "pmjc_approval_status", {"approval_id": aid}))["status"] == "unknown"
    admin = caller("boss", scopes=("admin",))
    assert (await executor.call_tool(admin, "pmjc_approval_status", {"approval_id": aid}))["status"] == "pending"
    out = await executor.call_tool(admin, "pmjc_approval_wait", {"approval_id": aid, "timeout_seconds": 0})
    assert out["status"] == "pending"
    mine = await executor.call_tool(caller("agent"), "pmjc_approval_status", {"approval_id": aid})
    assert mine["status"] == "pending"


async def test_cross_process_decision_is_seen_by_polling():
    """Another process decides: only the database changes, no in-process event fires."""
    from pmjc import db, executor
    aid = (await request(caller()))["approval_id"]

    async def other_process():
        await asyncio.sleep(0.15)
        db.x("UPDATE approvals SET status='done', result=? WHERE id=?", json.dumps({"sent": "a"}), aid)
    t = asyncio.create_task(other_process())
    out = await executor.wait_for_decision(aid, 3)
    await t
    assert out["status"] == "done" and out["result"] == {"sent": "a"}


async def test_expiry_is_detected_while_waiting():
    from pmjc import db, executor
    aid = (await request(caller()))["approval_id"]
    db.x("UPDATE approvals SET expires=? WHERE id=?", time.time() - 1, aid)
    out = await executor.wait_for_decision(aid, 2)
    assert out["status"] == "expired"
    assert executor.approval_status(aid)["status"] == "expired"


async def test_cancelling_the_wait_stops_it_and_drops_the_heartbeat():
    from pmjc import executor
    aid = (await request(caller()))["approval_id"]
    t = asyncio.create_task(executor.wait_for_decision(aid, 30))
    await asyncio.sleep(0.2)
    assert executor.has_live_waiter(aid)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert not executor.has_live_waiter(aid) and not executor._waiters


async def test_unknown_approval_returns_unknown():
    from pmjc import executor
    assert (await executor.wait_for_decision("nope", 1))["status"] == "unknown"


# --- no duplicate follow-ups ----------------------------------------------------------------------------
def make_approval(aid, status="done"):
    from pmjc import db, memory
    tid = memory.thread_new("send it", by="t")
    db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires,thread_id) "
         "VALUES(?,?,?,?,?,?,?,?,?,?)", aid, "fake_send", "{}", "h", "t", "", status, time.time(), time.time() + 60, tid)
    return tid


def fake_ask(monkeypatch):
    from pmjc import ask
    seen = []

    async def fn(c, text, **kw):
        seen.append(text)
        return {"answer": "followed up"}
    monkeypatch.setattr(ask, "ask", fn)
    return seen


async def test_continue_skips_model_run_when_delivered(monkeypatch):
    from pmjc import ask, executor, memory
    seen = fake_ask(monkeypatch)
    tid = make_approval("d1")
    executor.mark_delivered("d1")
    assert await ask.continue_after_approval("d1", {"status": "done", "result": "ok"}) is None
    assert not seen
    texts = [i["text"] for i in memory.thread_get(tid, mark_read=False)["items"]]
    assert any("delivered to the requesting client" in t for t in texts)


async def test_continue_skips_when_a_live_waiter_delivers_within_the_grace(monkeypatch):
    from pmjc import ask, executor
    seen = fake_ask(monkeypatch)
    make_approval("d2")
    executor._heartbeat("d2")

    async def deliver():
        await asyncio.sleep(0.2)
        executor.mark_delivered("d2")
    t = asyncio.create_task(deliver())
    assert await ask.continue_after_approval("d2", {"status": "done", "result": "ok"}) is None
    await t
    assert not seen


async def test_continue_runs_when_nobody_waited_or_the_waiter_vanished(monkeypatch):
    from pmjc import ask, executor
    seen = fake_ask(monkeypatch)
    make_approval("d3")
    assert await ask.continue_after_approval("d3", {"status": "done", "result": "ok"}) == "followed up"
    make_approval("d4")
    executor._heartbeat("d4")
    from pmjc import db
    db.x("UPDATE approvals SET waiter_seen=? WHERE id='d4'", time.time() - 60)  # waiter gave up long ago
    assert await ask.continue_after_approval("d4", {"status": "done", "result": "ok"}) == "followed up"
    assert len(seen) == 2


# --- ask/job/watch runs are not made to wait -------------------------------------------------------------
async def test_runs_do_not_wait_inline_or_get_the_wait_tool():
    from pmjc import ask, executor, mcp_server
    job = caller("job:x", kind="job")
    assert not mcp_server.is_mcp_client(job)
    assert not mcp_server.is_mcp_client(caller("ask:agent"), run_token=True)
    assert mcp_server.is_mcp_client(caller()) and mcp_server.is_mcp_client(caller("local", "local"))
    start = time.monotonic()
    res = await executor.call_tool(job, "fake_send", {"to": "a"})
    assert res["status"] == "pending_approval" and time.monotonic() - start < 1  # executor core never waits
    names = {t.name for t in ask.ask_tools(job)}
    assert "pmjc_approval_wait" not in names and "fake_send" in names
