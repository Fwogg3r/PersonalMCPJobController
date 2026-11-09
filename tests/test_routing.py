"""Front-door routing (phase4/ac): gpu_busy, interactive start, tiers/rules, sticky, log, footer, /v1 tiers."""
from __future__ import annotations

import asyncio
import types

import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME)


def _providers():
    from pmjc import config
    return [config.ProviderConfig(id="loc", kind="openai_compat", base_url="http://127.0.0.1:9/v1", trust="local",
                                  model="m", start="x"),
            config.ProviderConfig(id="plan", kind="claude_cli", model="sonnet", capabilities=["tools"]),
            config.ProviderConfig(id="api", kind="anthropic", model="x", credential="k", capabilities=["tools"])]


@pytest.fixture
def cfg(home):
    from pmjc import db, watches
    db.x(watches.DDL)
    from pmjc import config
    s = config.load()
    s.routing = _providers()
    config.save(s)
    return s


def _set(**kw):
    from pmjc import config
    s = config.load()
    for k, v in kw.items():
        setattr(s.router, k, v)
    config.save(s)


def caller(surface="discord", name="discord:1"):
    from pmjc.auth import Caller
    return Caller(name, "remote", {"sources"}, surface=surface)


# --- router fixes ---------------------------------------------------------------------------------------------
def _fake_smi(monkeypatch, util, apps):
    from pmjc import router
    monkeypatch.setattr(router.shutil, "which", lambda n: "nvidia-smi")

    def run(argv, **kw):
        out = f"{util}\n" if "--query-gpu=utilization.gpu" in argv[1] else "".join(f"{pid}, {n}, 100\n" for pid, n in apps)
        return types.SimpleNamespace(stdout=out, returncode=0)
    monkeypatch.setattr(router.subprocess, "run", run)


def _procs(monkeypatch, names):
    from pmjc import router
    monkeypatch.setattr(router, "_matching", lambda needles: sorted(x for x in names if any(n.lower() in x.lower() for n in needles if n)))


def test_gpu_busy_rules(monkeypatch, home):
    from pmjc import router
    # (a) a listed game while LM Studio runs = busy, even at low utilization
    _fake_smi(monkeypatch, 5, [(1, "LM Studio.exe")])
    _procs(monkeypatch, ["LM Studio.exe", "MyGame.exe"])
    assert router.gpu_busy(50, ["mygame"]) is True
    # nothing listed, no high utilization: not busy
    assert router.gpu_busy(50, []) is False
    # (b) high utilization with no pmjc request in flight = busy
    _fake_smi(monkeypatch, 90, [(1, "LM Studio.exe")])
    assert router.gpu_busy(50, []) is True
    _fake_smi(monkeypatch, 10, [])
    assert router.gpu_busy(50, []) is False
    # (c) high utilization while pmjc's own request is in flight = not busy
    _fake_smi(monkeypatch, 95, [(1, "LM Studio.exe")])
    with router.local_inflight():
        assert router.local_requests() == 1
        assert router.gpu_busy(50, []) is False
        assert router.gpu_busy(50, ["mygame"]) is True  # (a) still wins
    assert router.local_requests() == 0
    # no nvidia-smi: only the process list applies
    monkeypatch.setattr(router.shutil, "which", lambda n: None)
    assert router.gpu_busy(50, []) is False
    assert router.gpu_busy(50, ["mygame"]) is True


def test_gpu_busy_processes_from_config(monkeypatch, home):
    from pmjc import router
    _fake_smi(monkeypatch, 0, [])
    _procs(monkeypatch, ["launcher.exe"])
    assert router.gpu_busy(50) is False
    _set(gpu_busy_processes=["LAUNCHER"])
    assert router.gpu_busy(50) is True


def test_running_falls_back_to_tasklist(monkeypatch, home):
    import builtins
    from pmjc import router
    real = builtins.__import__

    def no_psutil(n, *a, **k):
        if n == "psutil":
            raise ImportError(n)
        return real(n, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_psutil)
    out = "game.exe\n"
    monkeypatch.setattr(router.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=out, returncode=0))
    assert router._running(["GAME"]) is True
    assert router._running(["other"]) is False


async def test_interactive_does_not_block_and_starts_in_background(monkeypatch, cfg):
    from pmjc import config, router
    s = config.load(); s.auto_start_local = True; config.save(s)
    started = []
    gate = asyncio.Event()

    async def fake_healthy(p, start=True):
        if start:
            started.append(p.id)
            await gate.wait()
            return True
        return False
    monkeypatch.setattr(router, "healthy", fake_healthy)
    p = config.load().routing[0]
    need = router.Need(interactive=True)
    assert await asyncio.wait_for(router.usable(p, need), 2) is False  # returned at once
    await asyncio.sleep(0)
    assert started == ["loc"]
    assert await router.usable(p, need) is False
    await asyncio.sleep(0)
    assert started == ["loc"]  # deduplicated while the first start is running
    gate.set()
    await asyncio.sleep(0.05)


async def test_jobs_keep_blocking_start(monkeypatch, cfg):
    from pmjc import config, router
    seen = []

    async def h(p, start=True):
        seen.append(start)
        return True
    monkeypatch.setattr(router, "healthy", h)
    assert await router.usable(config.load().routing[0], router.Need()) is True
    assert seen == [True]


async def test_agent_run_skips_down_local_host_for_interactive(monkeypatch, cfg):
    from pmjc import agent, router
    from pmjc.auth import Caller
    kicked = []
    monkeypatch.setattr(router, "kick_start", lambda p: kicked.append(p.id) or True)

    async def h(p, start=True):
        return p.id != "loc"
    monkeypatch.setattr(router, "healthy", h)

    async def fake_cli(p, *a, **k):
        return agent.Result("ok", p.id)
    monkeypatch.setattr(agent, "_claude_cli", fake_cli)
    res = await agent.run("hi", Caller("t", "remote", set()), [], router.Need(interactive=True, capabilities=set()))
    assert res.provider == "plan" and kicked == ["loc"]


# --- tiers and rules ------------------------------------------------------------------------------------------
def test_tier_defaults_and_custom(cfg):
    from pmjc import routing
    t = routing.tiers()
    assert t == {"default": ["loc", "plan", "api"], "strong": ["plan", "api"], "local": ["loc"]}
    _set(tiers={"cheap": ["loc", "nope", "api"], "strong": ["api"]})
    t = routing.tiers()
    assert t["cheap"] == ["loc", "api"] and t["strong"] == ["api"]  # unknown id dropped, built-in replaced


def test_rule_order_prefix_and_strip(cfg):
    from pmjc import config, routing
    R = config.RouteRule
    _set(route_rules=[R(prefix="/strong", tier="strong"), R(name="code", keywords=["python", "bug"], tier="strong"),
                      R(name="long", min_chars=50, tier="local"), R(name="cli", surface="cli", tier="local")])
    p = routing.plan(caller(), "/strong why is the sky blue")
    assert (p.text, p.tier, p.rule, p.override) == ("why is the sky blue", "strong", "/strong", True)
    assert p.order == ["plan", "api"]
    assert routing.plan(caller(), "/strongest thing").rule == "default"  # prefix is a whole word
    assert routing.plan(caller(), "/strong").text == "/strong"  # nothing left to ask: not a prefix
    p = routing.plan(caller(), "Fix this Python script")
    assert (p.tier, p.rule, p.override) == ("strong", "code", False)
    assert routing.plan(caller(), "pythonic thing").rule == "default"  # whole word only
    assert routing.plan(caller(), "x" * 60).rule == "long"  # first match wins over later rules
    assert routing.plan(caller("cli", "ask:cli"), "short").rule == "cli"
    assert routing.plan(caller("rest", "ask:x"), "short").rule == "default"
    p = routing.plan(caller(), "short chat")
    assert p.tier == "default" and p.order == ["loc", "plan", "api"]


def test_rule_with_unknown_tier_is_ignored(cfg):
    from pmjc import config, routing
    _set(route_rules=[config.RouteRule(name="bad", keywords=["hi"], tier="nope")])
    assert routing.plan(caller(), "hi there").rule == "default"


async def test_routing_never_widens_candidates(cfg):
    from pmjc import config, router, routing
    _set(route_rules=[config.RouteRule(prefix="/strong", tier="strong"),
                      config.RouteRule(keywords=["hard"], tier="strong")])
    for text in ("/strong anything", "this is hard"):
        p = routing.plan(caller(), text)
        need = router.Need(private=True)
        await routing.apply(p, need)
        assert need.order == ["loc"]  # private stays local, with or without the rule
    p = routing.plan(caller(), "/strong anything")
    need = router.Need()
    await routing.apply(p, need)
    assert need.order == ["plan", "api"]
    p = routing.plan(caller(), "x", provider_order=["api", "ghost", "loc"])  # an explicit order is also re-checked
    need = router.Need(private=True)
    await routing.apply(p, need)
    assert need.order == ["loc"]


# --- sticky, log, footer through ask() ------------------------------------------------------------------------
@pytest.fixture
def run(cfg, monkeypatch):
    """ask() with agent.run faked: records the Need it got and answers as the first candidate."""
    from pmjc import agent, ask as ask_mod
    class Seen(list):
        fb: list[str]
        pick: str = ""  # answer as this provider instead of the first in the order
    seen = Seen()
    seen_fb: list[str] = []  # fallbacks the fake reports for the next answers

    async def fake_run(prompt, c, tools, need, system=None, max_turns=12, **k):
        from pmjc import router as _r
        seen.append({"prompt": prompt, "order": list(need.order or []), "interactive": need.interactive,
                     "gpu_override": _r.gpu_override.get()})
        if c.on_tool:
            await c.on_tool("web_search", {}, "ok")
            await c.on_tool("web_fetch", {}, "error")
        return agent.Result("answer", seen.pick or need.order[0], 0.0123, 2, list(seen_fb))
    monkeypatch.setattr(agent, "run", fake_run)

    async def go(text, c=None, **kw):
        return await ask_mod.ask(c or caller(), text, **kw)
    seen.fb = seen_fb
    return go, seen


async def test_sticky_provider_and_override(run):
    from pmjc import config, routing
    go, seen = run
    _set(route_rules=[config.RouteRule(prefix="/strong", tier="strong"), config.RouteRule(prefix="/local", tier="local"),
                      config.RouteRule(name="hard", keywords=["hard"], tier="strong")])
    r1 = await go("/strong first question")
    tid = r1["thread_id"]
    assert seen[0]["order"] == ["plan", "api"] and r1["provider"] == "plan"
    assert seen[0]["prompt"] == "first question"  # prefix stripped
    assert seen[0]["interactive"] is True
    await go("follow up", thread_id=tid, history_turns=3)
    assert seen[1]["order"][0] == "plan"  # no prefix: sticky holds
    r2 = await go("follow up 2", thread_id=tid, history_turns=3)
    assert r2["routing"]["rule"] == "sticky"
    await go("/local back to local", thread_id=tid, history_turns=3)
    assert seen[3]["order"] == ["loc"]  # override replaces the sticky choice
    r4 = await go("again", thread_id=tid, history_turns=3)
    assert seen[4]["order"][0] == "loc" and r4["routing"]["rule"] == "sticky"
    # a thread that starts on its own rule keeps it too
    r5 = await go("this is hard")
    assert r5["routing"]["tier"] == "strong"
    await go("hi", thread_id=r5["thread_id"], history_turns=3)
    assert seen[-1]["order"][0] == "plan"
    # an invalid sticky choice (provider disabled) is replaced
    s = config.load()
    for p in s.routing:
        if p.id == "plan":
            p.enabled = False
    config.save(s)
    r7 = await go("hello again", thread_id=r5["thread_id"], history_turns=3)
    assert seen[-1]["order"][0] != "plan" and r7["routing"]["rule"] != "sticky"
    assert routing.tiers()["default"] == ["loc", "api"]


async def test_history_alone_does_not_make_thread_private(run):
    go, seen = run
    r1 = await go("first")
    await go("second", thread_id=r1["thread_id"], history_turns=3)
    assert seen[1]["order"][0] == "loc" and "plan" in seen[1]["order"]


async def test_routing_log_and_retry_of(run):
    from pmjc import db, routing
    go, seen = run
    seen.fb.append("x: boom")
    r1 = await go("what is X")
    tid = r1["thread_id"]
    seen.fb.clear()
    rows = db.q("SELECT * FROM routing")
    assert len(rows) == 1 and rows[0]["thread_id"] == tid and rows[0]["surface"] == "discord"
    assert rows[0]["rule"] == "default" and rows[0]["tier"] == "default" and rows[0]["provider"] == "loc"
    assert rows[0]["turns"] == 2 and rows[0]["tool_errors"] == 1 and abs(rows[0]["cost_usd"] - 0.0123) < 1e-9
    assert rows[0]["retry_of"] is None and "web_search" in rows[0]["tools"] and "x: boom" in rows[0]["fallbacks"]
    assert '"loc"' in rows[0]["candidates"]
    await go("/strong what is X", thread_id=tid, history_turns=3)
    rows = db.q("SELECT * FROM routing ORDER BY id")
    assert rows[1]["retry_of"] == rows[0]["id"] and rows[1]["rule"] == "/strong" and rows[1]["override"] == 1
    await go("something else", thread_id=tid, history_turns=3)
    assert db.q("SELECT retry_of FROM routing ORDER BY id")[2]["retry_of"] is None
    s = routing.summary(1)
    assert s["requests"] == 3 and s["retries"] == 1 and s["misrouted_rows"] == [rows[0]["id"]]


async def test_failed_run_is_logged(cfg, monkeypatch):
    from pmjc import agent, ask as ask_mod, db

    async def boom(*a, **k):
        raise agent.NoProvider("none")
    monkeypatch.setattr(agent, "run", boom)
    with pytest.raises(agent.NoProvider):
        await ask_mod.ask(caller(), "hi")
    r = db.q("SELECT provider, error FROM routing")
    assert r[0]["provider"] == "" and "NoProvider" in r[0]["error"]


def test_footer_added_and_stripped(cfg):
    from pmjc import ask as ask_mod, memory, routing
    line = routing.footer_line("local-27b", {"rule": "/strong", "tools": ["web_search", "web_fetch"]}, 0)
    assert line.endswith("local-27b \u00b7 rule: /strong \u00b7 tools: web_search, web_fetch \u00b7 $0.000")
    assert line.startswith(routing.FOOTER_MARK)
    assert routing.strip_footer("hello\n\n" + line) == "hello"
    assert routing.strip_footer("hello\n-# " + line) == "hello"
    assert routing.strip_footer("plain answer") == "plain answer"
    assert routing.strip_footer("a\n\n" + line + "\nmore") == "a\n\n" + line + "\nmore"  # only a trailing footer
    tid = memory.thread_new("t")
    memory.thread_add(tid, "request", "q")
    memory.thread_add(tid, "answer", "the answer\n\n" + line)
    past, _, _ = ask_mod.history(tid, 3)
    assert routing.FOOTER_MARK not in past and "You: the answer" in past
    assert routing.footer_enabled("discord") and routing.footer_enabled("v1")
    assert not routing.footer_enabled("rest") and not routing.footer_enabled("cli")
    _set(footer=False)
    assert not routing.footer_enabled("discord")


# --- /v1 ------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(cfg, monkeypatch):
    from fastapi.testclient import TestClient
    from pmjc import ask as ask_mod, auth, memory, openai_door, server
    openai_door._hits.clear()
    calls = []

    async def fake_ask(caller, text, private=False, provider_order=None, thread_id=None, history_turns=0, extra_system=""):
        calls.append({"order": provider_order, "tier": caller.tier_hint, "surface": caller.surface})
        tid = thread_id or memory.thread_new(text, by=caller.name)
        return {"answer": "hello", "provider": "loc", "watches": [], "thread_id": tid, "cost_usd": 0.0,
                "fell_back": [], "routing": {"rule": "default", "tools": ["web_search"]}}
    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    h = {"Authorization": "Bearer " + auth.create_token("phone", ["sources", "jobs", "actions"])}
    return c, h, calls


def test_v1_tier_models_and_footer(api):
    from pmjc import routing
    c, h, calls = api
    ids = [m["id"] for m in c.get("/v1/models", headers=h).json()["data"]]
    assert {"pmjc:default", "pmjc:strong", "pmjc:local", "pmjc:loc", "pmjc:plan"} <= set(ids)
    body = {"model": "pmjc:strong", "messages": [{"role": "user", "content": "hi"}]}
    r = c.post("/v1/chat/completions", json=body, headers=h).json()
    assert calls[-1] == {"order": ["plan", "api"], "tier": "strong", "surface": "v1"}
    content = r["choices"][0]["message"]["content"]
    assert content.startswith("hello") and routing.FOOTER_MARK in content.splitlines()[-1]
    assert "tools: web_search" in content and "rule: default" in content
    c.post("/v1/chat/completions", json={**body, "model": "pmjc:api"}, headers=h)
    assert calls[-1]["order"][0] == "api" and calls[-1]["tier"] == ""  # a provider id, not a tier
    _set(footer=False)
    r = c.post("/v1/chat/completions", json=body, headers=h).json()
    assert routing.FOOTER_MARK not in r["choices"][0]["message"]["content"]


def test_v1_conversation_key_ignores_footer(api):
    from pmjc import openai_door, routing
    plain = openai_door.pair_key("t", "hi", "hello")
    withf = openai_door.pair_key("t", "hi", "hello\n\n" + routing.footer_line("p", {"rule": "x"}, 0.01))
    assert plain == withf
