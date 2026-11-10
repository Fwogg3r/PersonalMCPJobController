"""GPU busy -> ask the owner (not the cloud), jobs wait, and the sticky provider's stay / switch-back rule (phase6/j)."""
from __future__ import annotations

import json

import pytest

from test_context import home  # noqa: F401  (autouse: temp PMJC_HOME)
from test_routing import _fake_smi, _procs, _set, caller, cfg, run  # noqa: F401  (fixtures and helpers)


BUSY = {"busy": True, "util": 91, "processes": ["mygame.exe"], "why": "a listed process is running"}
FREE = {"busy": False, "util": 3, "processes": [], "why": ""}


@pytest.fixture
def gpu(cfg, monkeypatch):
    """The local provider is GPU-guarded; the test flips `state` between BUSY and FREE."""
    from pmjc import config, router
    s = config.load()
    for p in s.routing:
        if p.id == "loc":
            p.extra = {"skip_when_gpu_busy_percent": 50}
    config.save(s)
    state = {"info": dict(BUSY), "ready": True}
    monkeypatch.setattr(router, "gpu_info", lambda thr, processes=None: dict(state["info"]))

    async def healthy(p, start=True):
        return state["ready"]
    monkeypatch.setattr(router, "healthy", healthy)
    return state


def _approvals():
    from pmjc import db
    return db.q("SELECT id, tool, status, args, thread_id FROM approvals WHERE tool='pmjc_route_override' ORDER BY created")


def _route(tid):
    from pmjc import db
    return db.q("SELECT * FROM thread_route WHERE thread_id=?", tid)[0]


# --- GPU busy: the owner chooses ------------------------------------------------------------------------------
async def test_busy_interactive_asks_and_runs_nothing(run, gpu):
    from pmjc import db
    go, seen = run
    r = await go("write me a haiku")
    assert seen == [] and r["provider"] == ""  # nothing ran
    rc = r["route_choice"]
    assert [o["id"] for o in rc["options"]] == ["local", "cloud", "wait"]
    assert "mygame.exe" in r["answer"] and "91%" in r["answer"] and "loc" in r["answer"]
    assert all(o["approve_url"].startswith("/approve/") and "sig=" in o["approve_url"] for o in rc["options"][:2])
    assert [a["status"] for a in _approvals()] == ["pending", "pending"]
    assert not db.q("SELECT 1 FROM thread_items WHERE thread_id=? AND kind='request'", r["thread_id"])
    log = db.q("SELECT rule, decision, provider FROM routing")[0]
    assert log["rule"] == "gpu busy" and log["decision"].startswith("gpu busy: asked") and log["provider"] == ""


async def test_choose_local_runs_the_request_with_the_gpu_guard_off(run, gpu):
    from pmjc import db, executor
    go, seen = run
    r = await go("write me a haiku")
    local = next(o for o in r["route_choice"]["options"] if o["id"] == "local")
    res = await executor.decide(local["approval_id"], True, "test")
    assert res["status"] == "done"
    assert [a["status"] for a in _approvals()].count("denied") == 1  # the cloud sibling is withdrawn
    r2 = await go("write me a haiku", thread_id=r["thread_id"], history_turns=3)  # what the door does next
    assert "route_choice" not in r2 and seen[0]["order"][0] == "loc" and seen[0]["gpu_override"] is True
    assert db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"] == "gpu busy: the user chose local anyway"
    r3 = await go("and again", thread_id=r["thread_id"], history_turns=3)  # the choice was for one run only
    assert "route_choice" in r3


async def test_choose_cloud_reruns_on_the_strong_tier(run, gpu):
    from pmjc import executor
    go, seen = run
    r = await go("write me a haiku")
    cloud = next(o for o in r["route_choice"]["options"] if o["id"] == "cloud")
    await executor.decide(cloud["approval_id"], True, "test")
    r2 = await go("write me a haiku", thread_id=r["thread_id"], history_turns=3)
    assert seen[0]["order"] == ["plan", "api"] and r2["routing"]["rule"] == "gpu busy: cloud"
    assert r2["routing"]["note"] == "fell back from loc: GPU busy"
    assert _route(r["thread_id"])["fell_back"] == 1 and _route(r["thread_id"])["planned_provider"] == "loc"


async def test_private_thread_offers_only_local_and_wait(run, gpu):
    go, seen = run
    r = await go("my diary", private=True)
    assert [o["id"] for o in r["route_choice"]["options"]] == ["local", "wait"]


async def test_continue_after_approval_runs_the_original_request(run, gpu):
    from pmjc import ask as ask_mod, db, executor
    go, seen = run
    r = await go("write me a haiku")
    cloud = next(o for o in r["route_choice"]["options"] if o["id"] == "cloud")
    res = await executor.decide(cloud["approval_id"], True, "test")
    answer = await ask_mod.continue_after_approval(cloud["approval_id"], res)  # REST / CLI / link path
    assert answer == "answer" and seen[0]["order"] == ["plan", "api"]
    assert db.q("SELECT text FROM thread_items WHERE thread_id=? AND kind='request'", r["thread_id"])[0]["text"] == "write me a haiku"
    deny = await go("another one")  # asks again; denying runs nothing
    res = await executor.decide(deny["route_choice"]["options"][0]["approval_id"], False, "test")
    assert await ask_mod.continue_after_approval(deny["route_choice"]["options"][0]["approval_id"], res) is None
    assert len(seen) == 1


async def test_action_cloud_and_local_skip_the_prompt(run, gpu):
    from pmjc import db
    go, seen = run
    _set(gpu_busy_action="cloud")
    r = await go("hello")
    assert "route_choice" not in r
    assert db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"].startswith("gpu busy: went to the cloud")
    r2 = await go("private note", private=True)  # private work never goes to the cloud: it is asked instead
    assert "route_choice" in r2 and [o["id"] for o in r2["route_choice"]["options"]] == ["local", "wait"]
    _set(gpu_busy_action="local")
    await go("hello again")
    assert seen[-1]["gpu_override"] is True
    assert db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"].startswith("gpu busy: ran local anyway")


async def test_free_gpu_and_explicit_choices_do_not_ask(run, gpu):
    go, seen = run
    gpu["info"] = dict(FREE)
    r = await go("hello")
    assert "route_choice" not in r and seen[0]["order"][0] == "loc"
    gpu["info"] = dict(BUSY)
    r2 = await go("/local hello")  # an explicit prefix is the owner's choice already
    assert "route_choice" not in r2


async def test_non_guarded_local_provider_never_asks(run, gpu, monkeypatch):
    from pmjc import config
    s = config.load()
    for p in s.routing:
        p.extra = {}
    config.save(s)
    go, seen = run
    assert "route_choice" not in await go("hello")


# --- jobs and watches -----------------------------------------------------------------------------------------
async def test_jobs_wait_by_default_and_never_go_cloud_when_private(cfg, gpu, monkeypatch):
    from pmjc import agent, auth, router
    got = []

    async def fake(prompt, caller_, tools, need, system, max_turns, expect_json, port, cands, tried):
        got.append((router.gpu_override.get(), [c.id for c in cands]))
        return agent.Result("ok", cands[0].id, 0.0, 1)
    monkeypatch.setattr(agent, "_run_candidates", fake)
    c = auth.Caller("job:x", "job", {"sources"})
    with pytest.raises(agent.GpuBusy):  # default: wait
        await agent.run("p", c, [], router.Need())
    _set(gpu_busy_jobs="cloud")
    await agent.run("p", c, [], router.Need())  # goes on; the guarded provider is skipped by healthy()
    with pytest.raises(agent.GpuBusy):  # private work never goes to the cloud
        await agent.run("p", c, [], router.Need(private=True))
    _set(gpu_busy_jobs="local")
    await agent.run("p", c, [], router.Need())
    assert got[-1][0] is True and router.gpu_override.get() is False  # reset afterwards
    gpu["info"] = dict(FREE)
    _set(gpu_busy_jobs="wait")
    await agent.run("p", c, [], router.Need())  # not busy: runs


async def test_door_choice_surfaces(run, gpu):
    from pmjc import discord_door, executor, openai_door
    go, seen = run
    r = await go("write me a haiku")
    rc = r["route_choice"]
    rows = discord_door.route_buttons(rc)[0]["components"]
    assert [b["label"] for b in rows] == ["Use local anyway", "Use a cloud model"]
    assert all(b["custom_id"].startswith(discord_door.BUTTON_PREFIX + "a:") for b in rows)
    block = openai_door.route_block(rc)
    assert "[Use local anyway](/approve/" in block and "[Use a cloud model](/approve/" in block
    assert openai_door.approvals_block(r["thread_id"], 0) == ""  # the generic approvals block skips the routing choices
    gpu["info"] = dict(FREE)
    assert "route_choice" not in await go("fine now")
    assert [a["status"] for a in _approvals()] == ["pending", "pending"]


# --- sticky provider: planned vs actual -----------------------------------------------------------------------
async def _fell_back_thread(go, seen, gpu):
    """A first turn that wanted the local provider but was answered by the cloud one."""
    gpu["info"] = dict(FREE)
    seen.pick = "plan"
    r = await go("first question")
    seen.pick = ""
    return r


async def test_fallback_is_remembered_with_planned_and_actual_and_footer(run, gpu):
    from pmjc import routing
    go, seen = run
    r = await _fell_back_thread(go, seen, gpu)
    row = _route(r["thread_id"])
    assert (row["provider"], row["planned_provider"], row["fell_back"]) == ("plan", "loc", 1) and row["reason"] == "not ready"
    foot = routing.footer_line(r["provider"], r["routing"], r["cost_usd"])
    assert foot.startswith("⟡ plan (fell back from loc: not ready) · ") and foot.endswith("reply /local to switch back")


async def test_switch_back_when_the_thread_is_easy(run, gpu):
    from pmjc import db, routing
    go, seen = run
    _set(stay_tool_calls=99)
    r = await _fell_back_thread(go, seen, gpu)
    r2 = await go("second", thread_id=r["thread_id"], history_turns=3)
    assert seen[-1]["order"][0] == "loc" and r2["provider"] == "loc" and r2["routing"]["rule"] == "switched back"
    foot = routing.footer_line(r2["provider"], r2["routing"], 0)
    assert foot.startswith("⟡ loc (switched back from plan) · ") and foot.endswith("/strong to stay on the cloud")
    assert "switch back to loc" in db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"]
    row = _route(r["thread_id"])
    assert (row["provider"], row["fell_back"]) == ("loc", 0)  # back on the plan: sticky as normal again
    r3 = await go("third", thread_id=r["thread_id"], history_turns=3)
    assert r3["routing"]["rule"] == "sticky" and r3["routing"]["note"] == ""


async def test_stay_when_the_last_run_used_many_tools(run, gpu):
    from pmjc import db, routing
    go, seen = run
    _set(stay_tool_calls=2)  # the fake run makes two tool calls
    r = await _fell_back_thread(go, seen, gpu)
    r2 = await go("second", thread_id=r["thread_id"], history_turns=3)
    assert seen[-1]["order"][0] == "plan" and r2["routing"]["rule"] == "sticky"
    assert db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"] == "stay on plan: the last run made 2 tool calls"
    foot = routing.footer_line(r2["provider"], r2["routing"], 0)
    assert foot.startswith("⟡ plan (fell back from loc: not ready)") and foot.endswith("reply /local to switch back")
    assert _route(r["thread_id"])["planned_provider"] == "loc"  # still remembered: the next turn may switch back


async def test_stay_when_strong_tier_condensed_or_long_history(run, gpu):
    from pmjc import config, db, memory
    go, seen = run
    _set(stay_tool_calls=99)
    # (1) the plan's tier was strong
    from pmjc import routing
    routing._init()
    t1 = memory.thread_new("one", by="t")
    db.x("INSERT INTO thread_route(thread_id,provider,tier,ts,planned_provider,planned_tier,fell_back,reason) "
         "VALUES(?,?,?,0,?,?,1,'down')", t1, "api", "strong", "plan", "strong")
    gpu["info"] = dict(FREE)
    await go("hi", thread_id=t1, history_turns=3)
    assert "the plan's tier was strong" in db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"]
    # (2) the history was condensed
    t2 = memory.thread_new("two", by="t")
    memory.thread_add(t2, "summary", "FACTS", author="pmjc", trust="agent")
    db.x("INSERT INTO thread_route(thread_id,provider,tier,ts,planned_provider,planned_tier,fell_back,reason) "
         "VALUES(?,?,?,0,?,?,1,'down')", t2, "plan", "default", "loc", "default")
    await go("hi", thread_id=t2, history_turns=3)
    assert "condensed" in db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"]
    # (3) the history is long relative to the local context (over 50% of context_tokens)
    s = config.load()
    for p in s.routing:
        if p.id == "loc":
            p.context_tokens = 1000
    config.save(s)
    t3 = memory.thread_new("three", by="t")
    memory.thread_add(t3, "answer", "w" * 4000, author="plan", trust="agent")  # ~1000 tokens
    db.x("INSERT INTO thread_route(thread_id,provider,tier,ts,planned_provider,planned_tier,fell_back,reason) "
         "VALUES(?,?,?,0,?,?,1,'down')", t3, "plan", "default", "loc", "default")
    await go("hi", thread_id=t3, history_turns=3)
    assert "of loc's context" in db.q("SELECT decision FROM routing ORDER BY id DESC")[0]["decision"]


async def test_planned_provider_still_down_replans(run, gpu):
    from pmjc import db
    go, seen = run
    r = await _fell_back_thread(go, seen, gpu)
    gpu["ready"] = False
    r2 = await go("second", thread_id=r["thread_id"], history_turns=3)
    assert r2["routing"]["rule"] != "sticky" and "still not available" in db.q(
        "SELECT decision FROM routing ORDER BY id DESC")[0]["decision"]


async def test_pivot_with_a_prefix_replaces_the_sticky_choice(run, gpu):
    go, seen = run
    _set(stay_tool_calls=2)
    r = await _fell_back_thread(go, seen, gpu)
    await go("second", thread_id=r["thread_id"], history_turns=3)  # stays on the cloud
    r3 = await go("/local third", thread_id=r["thread_id"], history_turns=3)  # the pivot in the footer
    assert seen[-1]["order"] == ["loc"] and r3["provider"] == "loc"
    row = _route(r["thread_id"])
    assert (row["provider"], row["planned_provider"], row["fell_back"]) == ("loc", "loc", 0)
    r4 = await go("/strong fourth", thread_id=r["thread_id"], history_turns=3)  # and the other way
    assert seen[-1]["order"] == ["plan", "api"] and _route(r["thread_id"])["provider"] == "plan"


async def test_private_thread_never_sticks_to_the_cloud(run, gpu, monkeypatch):
    from pmjc import db
    go, seen = run
    gpu["info"] = dict(FREE)
    seen.pick = "plan"  # a (misbehaving) answer from a cloud provider on a private thread
    r = await go("secret", private=True)
    assert not db.q("SELECT 1 FROM thread_route WHERE thread_id=?", r["thread_id"])
    seen.pick = ""
    r2 = await go("secret 2", thread_id=r["thread_id"], history_turns=3)
    assert seen[-1]["order"] == ["loc"]


def test_gpu_info_reports_processes_and_utilization(monkeypatch, home):
    from pmjc import router
    _fake_smi(monkeypatch, 88, [])
    _procs(monkeypatch, ["MyGame.exe", "LM Studio.exe"])
    info = router.gpu_info(50, ["mygame"])
    assert info["busy"] and info["processes"] == ["MyGame.exe"] and info["util"] == 88
    info = router.gpu_info(50, [])
    assert info["busy"] and info["why"] == "high utilization" and info["processes"] == []
