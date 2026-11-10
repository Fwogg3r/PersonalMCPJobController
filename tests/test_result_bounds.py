"""Bounded context inside a run (phase6/e): result caps with paging handles, and the tool-less condense pass."""
from __future__ import annotations

import json
import textwrap
import time

import httpx
import pytest

from pmjc import agent, auth, config, db, executor, memory, registry, results, router
from pmjc.config import ProviderConfig

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import creds
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    s = config.load()
    s.credential_backend = "file"
    config.save(s)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "big.py").write_text(textwrap.dedent('''
        from pmjc.connector import Connector, Field, tool

        class Big(Connector):
            id = "big"
            name = "Big"
            credentials = [Field("key", "Key")]

            @tool(scope="read")
            async def big_read(self, n: int = 10000) -> str:
                "A long trusted result."
                return "".join(str(i % 10) for i in range(n))

            @tool(scope="read", untrusted_output=True)
            async def big_web(self, n: int = 10000) -> str:
                "A long third-party result."
                return "w" * n
    '''))
    yield tmp_path
    db._local.c = None


def _reg():
    from pmjc import creds
    creds.store().set("big.key", "x")
    return registry.reload()


def _caller(thread=None):
    c = auth.Caller("t", "remote", {"sources"})
    c.thread_id = thread
    return c


def _prov(**kw):
    return ProviderConfig(id="fake", kind="openai_compat", base_url="http://fake/v1", model="m", trust="local", **kw)


class FakeLLM:
    """Scripted OpenAI-compatible server. `script` items are message dicts; condense calls (no tools) get `summary`."""

    def __init__(self, monkeypatch, script, summary="FACTS: x\nDONE: y\nLEFT: z", prompt_tokens=None):
        self.script, self.summary, self.requests, self.prompt_tokens = list(script), summary, [], prompt_tokens
        outer = self

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            outer.requests.append(body)
            if "tools" not in body:  # the condense pass
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": outer.summary}}]})
            msg = outer.script.pop(0)
            usage = {"prompt_tokens": outer.prompt_tokens(body)} if outer.prompt_tokens else {}
            return httpx.Response(200, json={"choices": [{"message": msg}], "usage": usage})

        monkeypatch.setattr(agent.httpx, "AsyncClient",
                            lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))

    @property
    def condense_requests(self):
        return [r for r in self.requests if "tools" not in r]


def call(name, args, cid="c1"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def final(text="done"):
    return {"role": "assistant", "content": text}


# --- caps --------------------------------------------------------------------------------------------------
def test_cap_derived_from_context_and_explicit_wins():
    cap = config.result_cap
    assert cap(_prov(context_tokens=8000)) == 4000
    assert cap(_prov(context_tokens=32000)) == 4000
    assert 4000 < cap(_prov(context_tokens=64000)) < 20000
    assert cap(_prov(context_tokens=200000)) == 20000
    assert cap(_prov(context_tokens=200000, max_result_chars=1500)) == 1500
    assert cap(_prov(context_tokens=8000, max_result_chars=9000)) == 9000


async def test_long_result_is_clipped_with_a_handle_and_pages(monkeypatch):
    reg = _reg()
    llm = FakeLLM(monkeypatch, [call("big_read", {"n": 10000}), final()])
    tools = [reg.tool("big_read")]
    caller = _caller()
    res = await agent._openai(_prov(context_tokens=32000), "go", caller, agent._with_paging(caller, tools), "sys", 5)
    assert res.text == "done"
    tool_msg = [m for m in llm.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert tool_msg.startswith("0123456789") and "[truncated: 6000 more chars; call pmjc_result_get(id=\"r_" in tool_msg
    assert any(t["function"]["name"] == "pmjc_result_get" for t in llm.requests[0]["tools"])
    rid = tool_msg.split('id="')[1].split('"')[0]
    s1 = await executor.call_tool(caller, "pmjc_result_get", {"id": rid, "offset": 4000, "max_chars": 3000})
    assert s1["offset"] == 4000 and len(s1["text"]) == 3000 and s1["remaining"] == 3000
    s2 = await executor.call_tool(caller, "pmjc_result_get", {"id": rid, "offset": 7000, "max_chars": 9000})
    assert len(s2["text"]) == 3000 and s2["remaining"] == 0 and "next" not in s2
    assert (s1["text"] + s2["text"]) == "".join(str(i % 10) for i in range(10000))[4000:]


async def test_page_slice_through_agent_is_capped_and_not_stored_again(monkeypatch):
    reg = _reg()
    caller = _caller()
    out = await agent._tool(caller, "big_read", {"n": 9000}, 1000)
    rid = out.split('id="')[1].split('"')[0]
    page = await agent._tool(caller, "pmjc_result_get", {"id": rid, "offset": 1000, "max_chars": 50000}, 1000)
    assert len(page) <= 1000 + len("...[truncated]")
    assert db.q("SELECT COUNT(*) AS n FROM results")[0]["n"] == 1


async def test_paging_taints_when_original_was_untrusted():
    _reg()
    # a later run in the same thread starts clean; reading the slice taints it
    tid = memory.thread_new("t")
    c1 = _caller(tid)
    out = await agent._tool(c1, "big_web", {"n": 9000}, 1000)
    assert c1.untrusted  # the original call tainted its own run
    rid = out.split('id="')[1].split('"')[0]
    c2 = _caller(tid)
    assert not c2.untrusted
    r = await executor.call_tool(c2, "pmjc_result_get", {"id": rid, "offset": 1000})
    assert r["text"] and c2.untrusted


async def test_paging_trusted_result_does_not_taint():
    _reg()
    memory.thread_new("t")
    tid = db.q("SELECT id FROM threads")[0]["id"]
    out = await agent._tool(_caller(tid), "big_read", {"n": 9000}, 1000)
    rid = out.split('id="')[1].split('"')[0]
    c2 = _caller(tid)
    await executor.call_tool(c2, "pmjc_result_get", {"id": rid})
    assert not c2.untrusted


async def test_other_thread_cannot_read_handle():
    _reg()
    a = memory.thread_new("a")
    b = memory.thread_new("b")
    out = await agent._tool(_caller(a), "big_read", {"n": 9000}, 1000)
    rid = out.split('id="')[1].split('"')[0]
    other = _caller(b)
    r = await executor.call_tool(other, "pmjc_result_get", {"id": rid})
    assert "error" in r and not other.untrusted
    assert "error" in await executor.call_tool(_caller(None), "pmjc_result_get", {"id": rid})
    assert "error" in await executor.call_tool(_caller(a), "pmjc_result_get", {"id": "r_nope"})
    assert "text" in await executor.call_tool(_caller(a), "pmjc_result_get", {"id": rid})  # same thread


async def test_handles_expire():
    _reg()
    caller = _caller()
    out = await agent._tool(caller, "big_read", {"n": 9000}, 1000)
    rid = out.split('id="')[1].split('"')[0]
    assert "text" in results.get(caller, rid)
    db.x("UPDATE results SET ts=?", time.time() - 25 * 3600)
    assert "error" in results.get(caller, rid)
    assert db.q("SELECT COUNT(*) AS n FROM results")[0]["n"] == 0
    s = config.load(); s.result_ttl_hours = 48; config.save(s)
    out = await agent._tool(caller, "big_read", {"n": 9000}, 1000)
    rid = out.split('id="')[1].split('"')[0]
    db.x("UPDATE results SET ts=?", time.time() - 25 * 3600)
    assert "text" in results.get(caller, rid)


# --- condense ----------------------------------------------------------------------------------------------
def _tokens_of(body):  # a provider that reports usage: the whole request, chars/4
    return len(json.dumps(body["messages"])) // 4 + 300


async def test_condense_triggers_keeps_system_request_latest_and_writes_thread(monkeypatch):
    reg = _reg()
    tid = memory.thread_new("work")
    caller = _caller(tid)
    llm = FakeLLM(monkeypatch, [call("big_web", {"n": 3000}, "a"), call("big_web", {"n": 3000}, "b"),
                                call("big_read", {"n": 3000}, "c"), final("all done")], prompt_tokens=_tokens_of)
    p = _prov(context_tokens=4000, reserve_tokens=500, max_result_chars=3000)  # soft limit 2450 tokens
    res = await agent._openai(p, "THE USER REQUEST", caller, [reg.tool("big_web"), reg.tool("big_read")], "SYSTEM PROMPT", 8)
    assert res.text == "all done"
    assert llm.condense_requests, "condense pass should have run"
    assert "tools" not in llm.condense_requests[0]
    # the request sent right after the condense pass
    idx = llm.requests.index(llm.condense_requests[0])
    nxt = llm.requests[idx + 1]["messages"]
    assert nxt[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert nxt[1]["role"] == "user" and nxt[1]["content"].startswith("THE USER REQUEST")
    assert "FACTS: x" in nxt[1]["content"]
    assert nxt[-2]["role"] == "assistant" and nxt[-1]["role"] == "tool"  # latest turn kept
    assert len(nxt) < len(llm.requests[idx - 1]["messages"]) + 1
    assert caller.untrusted  # flags unchanged by condensing
    items = memory.thread_get(tid)["items"]
    assert [i for i in items if i["kind"] == "summary" and "FACTS: x" in i["text"]]
    assert db.q("SELECT * FROM audit WHERE tool='condense' AND status='ok'")


async def test_condense_at_most_twice_then_stops_with_summary(monkeypatch):
    reg = _reg()
    script = [call("big_read", {"n": 3000}, f"c{i}") for i in range(8)]
    # over the soft limit until two condenses happened, then over the hard limit
    llm = FakeLLM(monkeypatch, script, prompt_tokens=lambda body: 1800 if len(llm.condense_requests) < 2 else 5000)
    p = _prov(context_tokens=4000, reserve_tokens=500, max_result_chars=3000)
    caller = _caller()
    res = await agent._openai(p, "req", caller, [reg.tool("big_read")], "sys", 10)
    assert len(llm.condense_requests) <= agent.MAX_CONDENSE
    assert res.text.startswith("(stopped:") and "FACTS: x" in res.text


async def test_no_condense_below_threshold(monkeypatch):
    reg = _reg()
    llm = FakeLLM(monkeypatch, [call("big_read", {"n": 100}), final()], prompt_tokens=lambda body: 500)
    p = _prov(context_tokens=32000)
    res = await agent._openai(p, "req", _caller(), [reg.tool("big_read")], "sys", 5)
    assert res.text == "done" and not llm.condense_requests
