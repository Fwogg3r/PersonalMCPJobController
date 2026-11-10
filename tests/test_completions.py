"""Streamed model calls: rebuilt messages, live answer/thinking, progress while the prompt is read, per-call log."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME)
from test_openai_door import api, body  # noqa: F401  (the /v1 client fixture)


def _sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def _delta(**d):
    return {"choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def _provider(**kw):
    from pmjc import config
    return config.ProviderConfig(id="loc", kind="openai_compat", base_url="http://127.0.0.1:9/v1", trust="local",
                                 model="m", **kw)


def _caller(untrusted=False):
    from pmjc.auth import Caller
    c = Caller("ask:t", "remote", {"sources"}, surface="v1")
    c.untrusted = untrusted
    c.deltas, c.notices = [], []

    async def d(kind, text):
        c.deltas.append((kind, text))

    async def n(text):
        c.notices.append(text)
    c.on_delta, c.on_notice = d, n
    return c


async def _call(payload: bytes, caller, delay=0.0, p=None):
    from pmjc import completions

    async def handler(req):
        if delay:
            await asyncio.sleep(delay)
        return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await completions.call(c, p or _provider(), {"model": "m", "messages": []}, {}, caller,
                                      prompt_tokens_est=12000, turn=1, tools_sent=3, schema_tokens=900)


async def test_answer_and_thinking_stream_and_the_call_is_logged(home):
    from pmjc import completions
    c = _caller()
    got = await _call(_sse(_delta(reasoning_content="let me "), _delta(reasoning_content="think"),
                           _delta(content="Hello "), _delta(content="there."),
                           {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7,
                                                     "completion_tokens_details": {"reasoning_tokens": 3}}}), c)
    assert got.message["content"] == "Hello there." and got.message["reasoning_content"] == "let me think"
    assert c.deltas == [("reasoning", "let me "), ("reasoning", "think"), ("content", "Hello "), ("content", "there.")]
    assert got.forwarded_all and got.ttft_s is not None
    row = completions.recent(1)[0]
    assert (row["provider"], row["prompt_tokens"], row["completion_tokens"], row["reasoning_tokens"],
            row["tools_sent"], row["status"]) == ("loc", 50, 7, 3, 3, "ok")


async def test_think_tags_split_across_chunks_become_reasoning(home):
    c = _caller()
    got = await _call(_sse(_delta(content="<thi"), _delta(content="nk>hmm</th"), _delta(content="ink>Answer")), c)
    assert got.message["content"] == "Answer" and got.message["reasoning_content"] == "hmm"
    assert ("reasoning", "hmm") in c.deltas and ("content", "Answer") in c.deltas
    assert not any("<" in t for _, t in c.deltas)


async def test_tool_calls_rebuilt_from_fragments(home):
    c = _caller()
    got = await _call(_sse(
        _delta(tool_calls=[{"index": 0, "id": "c1", "type": "function", "function": {"name": "weather", "arguments": ""}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '{"city":'}}]),
        _delta(tool_calls=[{"index": 1, "id": "c2", "function": {"name": "time", "arguments": "{}"}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": '"Oslo"}'}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}), c)  # LM Studio may say stop
    calls = got.message["tool_calls"]
    assert [(t["id"], t["function"]["name"], json.loads(t["function"]["arguments"])) for t in calls] == \
        [("c1", "weather", {"city": "Oslo"}), ("c2", "time", {})]


async def test_raw_tool_call_text_is_not_forwarded(home):
    c = _caller()
    got = await _call(_sse(_delta(content="Checking. "), _delta(content='<tool_call>{"name":"x"}</tool_call>')), c)
    assert c.deltas == [("content", "Checking. ")] and not got.forwarded_all


async def test_untrusted_run_streams_nothing(home):
    c = _caller(untrusted=True)
    got = await _call(_sse(_delta(content="see https://evil.example/?q=secret")), c)
    assert c.deltas == [] and not got.forwarded_all
    assert "evil.example" in got.message["content"]  # the answer still comes back, for the link check


async def test_progress_lines_while_the_prompt_is_read(home):
    from pmjc import config
    s = config.load()
    s.router.local_start_notice_seconds = 0.1
    config.save(s)
    c = _caller()
    await _call(_sse(_delta(content="ok")), c, delay=0.35)
    assert len(c.notices) >= 2
    assert all("reading the prompt (~12k tokens" in n for n in c.notices)


async def test_progress_uses_lm_studio_log_when_configured(home, tmp_path):
    from pmjc import config
    s = config.load()
    s.router.local_start_notice_seconds = 0.1
    config.save(s)
    logdir = tmp_path / "server-logs" / "2026-11"
    logdir.mkdir(parents=True)
    f = logdir / "2026-11-09.1.log"
    f.write_text("[x] Prompt processing progress: 99.0%\n")  # before the call: ignored

    async def grow():
        await asyncio.sleep(0.05)
        with open(f, "a") as fh:
            fh.write("[x][INFO][m] Prompt processing progress: 37.8%\n")
    c = _caller()
    t = asyncio.create_task(grow())
    await _call(_sse(_delta(content="ok")), c, delay=0.35, p=_provider(extra={"progress_log": str(tmp_path / "server-logs")}))
    await t
    assert any("LM Studio 38%" in n for n in c.notices)


async def test_failed_call_is_logged(home):
    from pmjc import completions

    async def handler(req):
        return httpx.Response(400, text="exceeds the available context size")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(RuntimeError, match="context size"):
            await completions.call(c, _provider(), {"model": "m", "messages": []}, {}, _caller(), prompt_tokens_est=10)
    row = completions.recent(1)[0]
    assert row["status"] == "error" and "context size" in row["error"]


async def test_plain_json_reply_still_works(home):
    from pmjc import completions
    payload = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

    async def handler(req):
        return httpx.Response(200, json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await completions.call(c, _provider(), {"model": "m", "messages": []}, {}, _caller(), prompt_tokens_est=10)
    assert got.message["content"] == "hi" and not got.forwarded_all
    assert completions.recent(1)[0]["prompt_tokens"] == 5


def test_lms_load_passes_parallel(home, monkeypatch):
    import os
    from pmjc import router
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    p = _provider(parallel=1, context_tokens=65536)
    argv = router._lms_load(p)
    assert argv[argv.index("--parallel") + 1] == "1" and "65536" in argv


def _stream_text(r):
    content, reasoning = "", ""
    for ln in r.text.splitlines():
        if ln.startswith("data: {"):
            d = json.loads(ln[6:])["choices"][0]["delta"]
            content += d.get("content", "") or ""
            reasoning += d.get("reasoning_content", "") or ""
    return content, reasoning


def _fake_ask(monkeypatch, streamed=True):
    from pmjc import ask as ask_mod, memory

    async def fake_ask(caller, text, private=False, provider_order=None, thread_id=None, history_turns=0, extra_system=""):
        await caller.on_delta("reasoning", "pondering")
        await caller.on_delta("content", "The answer.")
        tid = thread_id or memory.thread_new(text, by=caller.name)
        return {"answer": "The answer.", "provider": "loc", "watches": [], "thread_id": tid, "cost_usd": 0,
                "fell_back": [], "streamed": streamed}
    monkeypatch.setattr(ask_mod, "ask", fake_ask)


def test_v1_streams_answer_once_with_reasoning_field(api, monkeypatch):
    c, h, _ = api
    _fake_ask(monkeypatch)
    content, reasoning = _stream_text(c.post("/v1/chat/completions", headers=h, json=body("hi", stream=True)))
    assert reasoning == "pondering"
    assert content.count("The answer.") == 1


def test_v1_think_tags_mode(api, monkeypatch):
    from pmjc import config
    s = config.load()
    s.front_door.reasoning = "think_tags"
    config.save(s)
    c, h, _ = api
    _fake_ask(monkeypatch)
    content, reasoning = _stream_text(c.post("/v1/chat/completions", headers=h, json=body("hi", stream=True)))
    assert reasoning == "" and "<think>\npondering\n</think>" in content and content.count("The answer.") == 1


def test_v1_not_streamed_answer_is_sent_whole(api, monkeypatch):
    c, h, _ = api
    _fake_ask(monkeypatch, streamed=False)  # e.g. defanged: the live text went out, then the checked answer
    content, _ = _stream_text(c.post("/v1/chat/completions", headers=h, json=body("hi", stream=True)))
    assert content.count("The answer.") == 2


def test_v1_follow_up_finds_the_thread_after_streamed_text(api, monkeypatch):
    from pmjc import ask as ask_mod, memory
    c, h, _ = api
    seen = []

    async def fake_ask(caller, text, private=False, provider_order=None, thread_id=None, history_turns=0, extra_system=""):
        seen.append(thread_id)
        await caller.on_notice("_model reading the prompt (~3k tokens)… 15s_")
        await caller.on_delta("content", "Let me check. ")  # interim text of a tool turn
        await caller.on_delta("content", "Final words.")
        tid = thread_id or memory.thread_new(text, by=caller.name)
        return {"answer": "Final words.", "provider": "loc", "watches": [], "thread_id": tid, "cost_usd": 0,
                "fell_back": [], "streamed": True}
    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    first, _ = _stream_text(c.post("/v1/chat/completions", headers=h, json=body("hi", stream=True)))
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": first},
            {"role": "user", "content": "and then?"}]
    c.post("/v1/chat/completions", headers=h, json={"model": "pmjc", "messages": msgs, "stream": True})
    assert seen[0] is None and seen[1] is not None
