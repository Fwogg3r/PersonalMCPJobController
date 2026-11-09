"""Interactive requests wait for a local host to start when local is all that's left (phase4/ac).
The providers are fake: the local host is down at first and healthy after a short delay."""
from __future__ import annotations

import asyncio
import json

import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME)
from test_openai_door import api, body  # noqa: F401  (the /v1 client fixture)


@pytest.fixture
def env(home, monkeypatch):
    from pmjc import agent, config, router
    s = config.load()
    s.auto_start_local = True
    s.router.local_start_wait_seconds = 1.0
    s.routing = [config.ProviderConfig(id="loc", kind="openai_compat", base_url="http://127.0.0.1:9/v1", trust="local",
                                       model="m", start="x"),
                 config.ProviderConfig(id="api", kind="anthropic", model="x", credential="k", capabilities=["tools"])]
    config.save(s)
    router._starting.clear()
    st = {"up": False, "delay": 0.1, "starts": 0}

    async def healthy(p, start=True):
        if p.trust != "local":
            return True
        if st["up"]:
            return True
        if start:
            st["starts"] += 1
            await asyncio.sleep(st["delay"])
            st["up"] = True
            return True
        return False
    monkeypatch.setattr(router, "healthy", healthy)

    async def fake_openai(p, prompt, caller, tools, system, max_turns):
        return agent.Result("local answer", p.id)

    async def fake_anthropic(p, prompt, caller, tools, system, max_turns):
        return agent.Result("cloud answer", p.id)
    monkeypatch.setattr(agent, "_openai", fake_openai)
    monkeypatch.setattr(agent, "_anthropic", fake_anthropic)
    yield st
    for t in router._starting.values():
        t.cancel()
    router._starting.clear()


def _set(**kw):
    from pmjc import config
    s = config.load()
    for k, v in kw.items():
        if k == "auto_start_local":
            s.auto_start_local = v
        else:
            setattr(s.router, k, v)
    config.save(s)


def _caller(notices=None):
    from pmjc.auth import Caller
    c = Caller("ask:t", "remote", {"sources"}, surface="rest")
    if notices is not None:
        async def n(text):
            notices.append(text)
        c.on_notice = n
    return c


async def _run(order, private=False, c=None):
    from pmjc import agent, router
    need = router.Need(order=order, private=private, interactive=True, capabilities=set())
    return await agent.run("hi", c or _caller(), [], need)


async def test_local_only_waits_and_succeeds(env):
    notices = []
    r = await _run(["loc"], private=True, c=_caller(notices))
    assert r.text == "local answer" and r.provider == "loc"
    assert r.waited_s > 0.05 and env["starts"] == 1
    assert len(notices) == 1 and "starting the local model" in notices[0]


async def test_local_only_tier_waits(env):
    r = await _run(["loc"])  # a tier made only of local providers: the order has just loc
    assert r.provider == "loc" and r.fell_back == []


async def test_timeout_fails_with_the_message(env):
    from pmjc import agent
    env["delay"] = 5
    _set(local_start_wait_seconds=0.2)
    with pytest.raises(agent.NoProvider) as e:
        await _run(["loc"], private=True)
    assert "didn't start in 0.2 s" in str(e.value) and "try again" in str(e.value)


async def test_prefer_wait_zero_falls_back_at_once(env):
    r = await _run(["loc", "api"])
    assert r.provider == "api" and r.waited_s == 0
    await asyncio.sleep(0.3)
    assert env["up"] is True  # the start went on in the background


async def test_prefer_wait_waits_then_uses_local(env):
    _set(prefer_local_wait_seconds=1.0)
    r = await _run(["loc", "api"])
    assert r.provider == "loc" and r.waited_s > 0.05


async def test_prefer_wait_times_out_to_the_fallback(env):
    env["delay"] = 5
    _set(prefer_local_wait_seconds=0.2)
    r = await _run(["loc", "api"])
    assert r.provider == "api" and r.waited_s >= 0.15


async def test_concurrent_requests_share_one_start(env):
    env["delay"] = 0.3
    rs = await asyncio.gather(*[_run(["loc"], private=True) for _ in range(4)])
    assert all(r.provider == "loc" for r in rs)
    assert env["starts"] == 1


async def test_gpu_busy_guard_prevents_the_start(env, monkeypatch):
    from pmjc import agent, config, router
    s = config.load()
    s.routing[0].extra = {"skip_when_gpu_busy_percent": 50}
    config.save(s)
    monkeypatch.setattr(router, "gpu_busy", lambda thr, processes=None: True)
    with pytest.raises(agent.NoProvider):
        await _run(["loc"], private=True)
    assert env["starts"] == 0 and env["up"] is False


async def test_local_anyway_choice_starts_on_a_busy_gpu(env, monkeypatch):
    from pmjc import config, router
    s = config.load()
    s.routing[0].extra = {"skip_when_gpu_busy_percent": 50}
    config.save(s)
    monkeypatch.setattr(router, "gpu_busy", lambda thr, processes=None: True)
    tok = router.gpu_override.set(True)
    try:
        r = await _run(["loc"], private=True)
    finally:
        router.gpu_override.reset(tok)
    assert r.provider == "loc" and env["starts"] == 1


async def test_auto_start_off_fails_fast_naming_the_setting(env):
    from pmjc import agent
    _set(auto_start_local=False)
    with pytest.raises(agent.NoProvider) as e:
        await asyncio.wait_for(_run(["loc"], private=True), 0.5)
    assert "auto_start_local" in str(e.value) and env["starts"] == 0


async def test_footer_and_log_note_the_wait(env):
    from pmjc import db, routing
    r = await _run(["loc"], private=True)
    r.waited_s = 12.4
    p = routing.plan(_caller(), "hi")
    p.candidates = ["loc"]
    info = routing.finish(p, r, "t1")
    assert info["waited"] == 12
    assert "waited 12s for local start" in routing.footer_line("loc", info, 0)
    assert "waited 12s for local start" in db.q("SELECT decision FROM routing")[0]["decision"]


def test_v1_streaming_shows_the_progress_line(api, monkeypatch):
    from pmjc import agent, ask as ask_mod, memory
    c, h, _ = api

    async def fake_ask(caller, text, private=False, provider_order=None, thread_id=None, history_turns=0, extra_system=""):
        await caller.on_notice(agent.LOCAL_START_NOTICE)
        await asyncio.sleep(0.05)
        tid = thread_id or memory.thread_new(text, by=caller.name)
        return {"answer": "done", "provider": "loc", "watches": [], "thread_id": tid, "cost_usd": 0, "fell_back": []}
    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    r = c.post("/v1/chat/completions", headers=h, json=body("hi", stream=True))
    text = "".join(json.loads(ln[6:])["choices"][0]["delta"].get("content", "")
                   for ln in r.text.splitlines() if ln.startswith("data: {"))
    assert "starting the local model" in text and text.rstrip().endswith("done")
