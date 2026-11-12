"""Thinking effort per run (pmjc.effort): prefixes, fixed and adaptive choice, request fields, escalation."""
from __future__ import annotations

from test_tool_select import FakeLLM, _caller, _prov, _universe, call, final, home  # noqa: F401  (home: autouse fixture)

import json

from pmjc import agent, config, effort, memory, tool_select
from pmjc.config import ThinkingConfig


def _think(**kw):
    return ThinkingConfig(**{"control": "kwargs", **kw})


def test_prefix_strip():
    assert effort.strip_prefix("/fast skip this song") == ("skip this song", "off")
    assert effort.strip_prefix("/THINK  plan my week") == ("plan my week", "high")
    assert effort.strip_prefix("/fastest thing") == ("/fastest thing", None)
    assert effort.strip_prefix("play music") == ("play music", None)


def test_no_control_sends_nothing():
    p = _prov()
    assert effort.choose(p, "x", _caller()) is None
    body = {}
    effort.apply(body, p, None)
    assert body == {}


def test_fixed_and_prefix():
    p = _prov(thinking=_think(default="medium"))
    assert effort.choose(p, "anything", _caller()).level == "medium"
    c = _caller()
    c.effort_override = "off"
    assert effort.choose(p, "anything", c).level == "off"


def _selected(p, prompt):
    c = _caller()
    u = _universe(c)
    tool_select.begin(p, c, u, prompt)
    c.select_hint = prompt
    return c


def test_adaptive_uses_connector_hints(monkeypatch):
    p = _prov(thinking=_think(policy="adaptive"))
    monkeypatch.setattr(effort, "hint_of", lambda cid: {"alpha": "low", "beta": "high"}.get(cid, "medium"))
    assert effort.choose(p, "the lamp", _selected(p, "the lamp")).level == "low"
    assert effort.choose(p, "lamp and zebra", _selected(p, "lamp and zebra")).level == "high"
    assert effort.choose(p, "hello there", _selected(p, "hello there")).level == "medium"  # nothing selected
    assert effort.choose(p, "lamp then quartz", _selected(p, "lamp then quartz")).level == "medium"  # two connectors
    assert effort.choose(p, "debug the lamp", _selected(p, "debug the lamp")).level == "high"  # reads like building
    assert effort.choose(p, "the lamp", _selected(p, "the lamp"), tier="strong").level == "high"
    floor = _prov(thinking=_think(policy="adaptive", adaptive_floor="medium"))
    assert effort.choose(floor, "the lamp", _selected(floor, "the lamp")).level == "medium"


def test_owner_override(monkeypatch):
    s = config.load()
    s.router.connector_effort = {"alpha": "high"}
    monkeypatch.setattr(config, "load", lambda: s)
    assert effort.hint_of("alpha") == "high"


def test_apply_controls():
    e = effort.Effort("high", "x")
    body = {}
    effort.apply(body, _prov(thinking=_think(levels={"high": "xhigh"})), e)
    assert body["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "xhigh"}
    body = {}
    effort.apply(body, _prov(thinking=_think()), effort.Effort("off", "x"))
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    body = {}
    effort.apply(body, _prov(thinking=ThinkingConfig(control="param")), effort.Effort("low", "x"))
    assert body == {"reasoning_effort": "low"}
    body = {}
    effort.apply(body, _prov(thinking=ThinkingConfig(control="param")), effort.Effort("off", "x"))
    assert body == {"reasoning_effort": "none"}


def test_failure_signals_and_single_escalation():
    e = effort.Effort("low", "x")
    assert e.failure("a", {}, "ok") is None
    assert e.failure("a", {}, "ok") == "a called twice with the same arguments"
    assert e.failure("b", {}, "ERROR: boom") == "b failed"
    assert e.failure("c", {}, '{"error": "Denied: no"}') == "c failed"
    assert e.escalate("b failed", True) and e.level == "medium"
    assert not e.escalate("again", True) and e.level == "medium"  # once per run
    assert not effort.Effort("low", "x").escalate("y", False)


async def test_loop_sends_level_and_escalates(monkeypatch):
    llm = FakeLLM(monkeypatch, [call("alpha_read", {"q": "x"}, "c1"), call("nope_tool", {}, "c2"), final()])
    c = _caller(memory.thread_new("t"))
    u = _universe(c)
    p = _prov(thinking=_think(policy="adaptive"))
    monkeypatch.setattr(effort, "hint_of", lambda cid: "low")
    c.select_hint = "the lamp"
    res = await agent._openai(p, "the lamp", c, u, "sys", 6)
    assert res.text == "done"
    kw = [r["chat_template_kwargs"] for r in llm.requests]
    assert kw[0] == {"enable_thinking": True, "reasoning_effort": "low"}
    assert kw[1] == kw[0]
    assert kw[2]["reasoning_effort"] == "medium"  # the unknown tool's error raised it for the rest of the run
    assert c.effort.escalated


async def test_transient_engine_error_is_retried_once(monkeypatch):
    import httpx
    from test_tool_select import _REAL_CLIENT
    seen = []

    def handler(req):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(500, text='{"error":"Engine protocol predict request failed: fetch failed"}')
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})

    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(agent, "TRANSIENT_RETRY_SECONDS", 0)
    c = _caller(memory.thread_new("t"))
    res = await agent._openai(_prov(mode="all"), "hi", c, [], "sys", 3)
    assert res.text == "ok" and len(seen) == 2


def _multi(*calls_):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls_)]}


async def test_parallel_reads_only_without_egress(monkeypatch):
    import asyncio
    seen = []
    real = agent.asyncio.gather

    async def spy(*aws, **kw):
        seen.append(len(aws))
        return await real(*aws, **kw)

    monkeypatch.setattr(agent.asyncio, "gather", spy)
    FakeLLM(monkeypatch, [_multi(("alpha_read", {"q": "a"}), ("gamma_one", {"x": "b"})),
                          _multi(("alpha_read", {"q": "c"}), ("beta_net", {"url": "u"})), final()])
    c = _caller(memory.thread_new("t"))
    await agent._openai(_prov(mode="all"), "x", c, _universe(c), "sys", 6)
    assert seen == [2]  # the second response holds an egress tool: sequential


async def test_repeat_note_and_last_turn_note(monkeypatch):
    llm = FakeLLM(monkeypatch, [call("alpha_read", {"q": "a"}, "c1"), call("alpha_read", {"q": "a"}, "c2"), final()])
    c = _caller(memory.thread_new("t"))
    await agent._openai(_prov(mode="all"), "x", c, _universe(c), "sys", 3)
    results = llm.tool_results(2)
    assert not results[0].startswith("[You already") and results[1].startswith("[You already made this exact call")
    assert llm.requests[2]["messages"][-1]["content"].startswith("[pmjc] This is your last model call")


def test_yaml_off_is_a_level_not_false():
    import yaml
    t = ThinkingConfig.model_validate(yaml.safe_load("control: param\ndefault: off\nadaptive_floor: off"))
    assert t.default == "off" and t.adaptive_floor == "off"
    r = config.RouterConfig.model_validate(yaml.safe_load("effort_prefixes: {/quick: off}\nconnector_effort: {ssh: off}"))
    assert r.effort_prefixes == {"/quick": "off"} and r.connector_effort == {"ssh": "off"}
