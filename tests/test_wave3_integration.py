"""Cross-feature check (routing + tool_mode select + pmjc_research + footer) through the /v1 door, with fakes only."""
from __future__ import annotations

import pytest

from pmjc import config, db, memory, research, router, routing
from pmjc.config import ProviderConfig

from test_research import home  # noqa: F401  (autouse: temp PMJC_HOME plus the fake web connector)
from test_result_bounds import FakeLLM, call, final

A = "https://one.example.org/a"


@pytest.fixture
def api(home, monkeypatch):
    from fastapi.testclient import TestClient
    from pmjc import auth, openai_door, server, watches
    db.x(watches.DDL)
    openai_door._hits.clear()
    s = config.load()
    s.routing = [ProviderConfig(id="loc", kind="openai_compat", base_url="http://fake/v1", model="m", trust="local",
                                tool_mode="select", capabilities=["tools"])]
    s.router.route_rules = [config.RouteRule(name="grinders", keywords=["grinder"], tier="local")]
    config.save(s)

    async def healthy(p, start=True):
        return True
    monkeypatch.setattr(router, "healthy", healthy)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    h = {"Authorization": "Bearer " + auth.create_token("phone", ["sources", "jobs", "actions"])}
    return c, h


def _chat(c, h, messages):
    r = c.post("/v1/chat/completions", json={"model": "pmjc", "messages": messages}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["choices"][0]["message"]["content"]


def test_select_mode_routing_research_taint_and_footer(api, monkeypatch):
    c, h = api
    llm = FakeLLM(monkeypatch, [
        call("pmjc_research", {"question": "which grinder?"}),  # parent, turn 1
        call("web_search", {"queries": ["grinder"]}),  # sub-run
        final(f"The Nimbus G2 is best ({A})."),  # sub-run answer
        final("Buy the Nimbus G2."),  # parent answer
        final("It was the Nimbus."),  # second turn
    ])
    a1 = _chat(c, h, [{"role": "user", "content": "Which grinder should I buy?"}])
    assert a1.startswith("Buy the Nimbus G2.")
    # the footer is present on the v1 surface, and shows the routing rule and the research tool
    foot = a1.split("\n")[-1]
    assert foot.startswith(routing.FOOTER_MARK) and "rule: grinders" in foot and "pmjc_research" in foot
    parent_req, sub_req = llm.requests[0], llm.requests[1]
    # routing picked the tier
    row = db.q("SELECT rule, tier, provider FROM routing ORDER BY id")[0]
    assert (row["rule"], row["tier"], row["provider"]) == ("grinders", "local", "loc")
    # the parent run is in select mode: the briefs block is in its system prompt
    assert "More tools exist but aren't loaded" in parent_req["messages"][0]["content"]
    # the sub-run holds only the web tools (+ paging), gets no briefs and no parent selection
    names = {t["function"]["name"] for t in sub_req["tools"]}
    assert names == {"web_search", "web_fetch", "pmjc_result_get"}
    assert "More tools exist" not in sub_req["messages"][0]["content"]
    assert sub_req["messages"][0]["content"].startswith(research.RESEARCH_SYSTEM[:40])
    # the parent ended up tainted: the thread's answer and research rows are untrusted
    tid = db.q("SELECT thread_id FROM thread_items WHERE kind='answer'")[0]["thread_id"]
    trust = {r["kind"]: r["trust"] for r in db.q("SELECT kind, trust FROM thread_items WHERE thread_id=?", tid)}
    assert trust["research"] == "untrusted" and trust["answer"] == "untrusted"
    # the next turn: the footer is stripped from the history the model sees
    msgs = [{"role": "user", "content": "Which grinder should I buy?"}, {"role": "assistant", "content": a1},
            {"role": "user", "content": "And which was it again?"}]
    a2 = _chat(c, h, msgs)
    assert a2.startswith("It was the Nimbus.")
    nxt = [r for r in llm.requests if "tools" in r][-1]
    assert routing.FOOTER_MARK not in nxt["messages"][1]["content"]
    assert "Buy the Nimbus G2." in nxt["messages"][1]["content"]
    assert routing.FOOTER_MARK not in "".join(r["text"] for r in db.q("SELECT text FROM thread_items WHERE thread_id=?", tid))


async def test_research_sub_run_respects_private_parent(home, monkeypatch):
    from pmjc import auth
    seen = []

    async def cands(need):
        seen.append(need.private)
        return [ProviderConfig(id="loc", kind="openai_compat", base_url="http://fake/v1", model="m", trust="local")]

    async def healthy(p, start=True):
        return True
    monkeypatch.setattr(router, "candidates", cands)
    monkeypatch.setattr(router, "healthy", healthy)
    FakeLLM(monkeypatch, [final("x"), final("y")])
    tid = memory.thread_new("t", by="p", private=True)
    memory.thread_make_private(tid)
    p = auth.Caller("agent", "remote", {"sources"})
    p.thread_id = tid
    await research.research(p, "q")
    assert seen == [True]
    p2 = auth.Caller("agent2", "remote", {"sources"})
    await research.research(p2, "q")
    assert seen == [True, False]
