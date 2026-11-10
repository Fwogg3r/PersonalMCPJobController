"""pmjc_research (phase6/h): the quarantined research sub-run. Fake provider, fake web tools, no network."""
from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from pmjc import agent, auth, config, db, executor, memory, registry, research, router
from pmjc.config import ProviderConfig

from test_result_bounds import FakeLLM, call, final

FAKEWEB = '''
from pmjc.connector import Connector, tool


class FakeWeb(Connector):
    id = "fakeweb"
    name = "Fake web"
    credentials = []

    @tool(scope="read", untrusted_output=True, egress=True, private_output=False)
    async def web_search(self, queries: list[str]) -> list[dict]:
        "Search."
        return [{"query": q, "rows": [{"title": "T", "url": "https://one.example.org/a", "snippet": "s"},
                                       {"title": "U", "url": "https://two.example.net/b", "snippet": "s"}]} for q in queries]

    @tool(scope="read", untrusted_output=True, egress=True, private_output=False)
    async def web_fetch(self, urls: list[str]) -> list[dict]:
        "Fetch."
        return [{"final_url": u, "title": "page", "text": "page text of " + u + " " + "w" * 50} for u in urls]

    @tool(scope="read")
    async def priv_notes(self) -> str:
        "Private notes (default: private output)."
        return "the user's secret"
'''


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
    s.disabled_connectors = ["web"]  # the real web connector would clash with the fake tool names
    config.save(s)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "fakeweb.py").write_text(textwrap.dedent(FAKEWEB))
    registry.reload()
    yield tmp_path
    db._local.c = None


PROV = ProviderConfig(id="fake", kind="openai_compat", base_url="http://fake/v1", model="m", trust="local")


@pytest.fixture
def needs(monkeypatch):
    seen = []

    async def cands(need):
        seen.append(need)
        return [PROV]

    async def healthy(p, start=True):
        return True
    monkeypatch.setattr(router, "candidates", cands)
    monkeypatch.setattr(router, "healthy", healthy)
    return seen


def parent(**kw):
    c = auth.Caller("agent", "remote", {"sources", "actions", "jobs"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def search(cid="c1"):
    return call("web_search", {"queries": ["q"]}, cid)


def fetch(*urls, cid="c2"):
    return call("web_fetch", {"urls": list(urls)}, cid)


A, B = "https://one.example.org/a", "https://two.example.net/b"


def denied_rows():
    return db.q("SELECT actor, tool, detail FROM audit WHERE status='denied'")


# --- the run ---------------------------------------------------------------------------------------------
async def test_search_then_fetch_then_fetch(monkeypatch, needs):
    llm = FakeLLM(monkeypatch, [search(), fetch(A), fetch(B, cid="c3"), final(f"Answer from {A} and {B}.")])
    p = parent()
    out = await executor.call_tool(p, "pmjc_research", {"question": "what is x?"})
    assert out["untrusted"] is True and out["summary"] == f"Answer from {A} and {B}."
    assert out["sources"] == [A, B]
    assert not denied_rows()  # egress after untrusted was allowed inside the quarantine
    rows = db.q("SELECT actor, tool, status FROM audit WHERE actor='research:agent' AND status='ok'")
    assert [r["tool"] for r in rows] == ["web_search", "web_fetch", "web_fetch"]  # audited under the research caller
    q = db.q("SELECT detail FROM audit WHERE tool='pmjc_research' AND status='question'")
    assert q[0]["detail"] == "what is x?"
    assert len(llm.requests) == 4


async def test_research_has_no_private_tools_and_no_model_md(monkeypatch, needs):
    memory.set_model_md("MODEL-MD-SECRET standing rules")
    memory.note_set("pin", "NOTE-SECRET", ["pinned"])
    llm = FakeLLM(monkeypatch, [final("nothing")])
    await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    req = llm.requests[0]
    names = {t["function"]["name"] for t in req["tools"]}
    assert names == {"web_search", "web_fetch", "pmjc_result_get"}
    blob = json.dumps(req)
    assert "MODEL-MD-SECRET" not in blob and "NOTE-SECRET" not in blob and "standing context" not in blob
    assert req["messages"][0] == {"role": "system", "content": research.RESEARCH_SYSTEM}
    assert [m["role"] for m in req["messages"]] == ["system", "user"]  # no thread history


async def test_research_run_can_not_call_private_or_memory_tools(monkeypatch, needs):
    sub = research.new_caller(parent())
    sub.allowed_tools = {t.name for t in research.public_tools()}
    for name, args in (("priv_notes", {}), ("pmjc_memory_get", {"key": "k"}), ("pmjc_context", {}),
                       ("pmjc_research", {"question": "q"})):
        with pytest.raises(executor.Denied):
            await executor.call_tool(sub, name, args)


async def test_config_cannot_add_private_tools(monkeypatch, needs):
    s = config.load()
    s.research.tools = ["web_search", "web_fetch", "priv_notes", "pmjc_memory_set", "nope"]
    config.save(s)
    assert [t.name for t in research.public_tools()] == ["web_search", "web_fetch"]


async def test_research_tools_config_and_turns(monkeypatch, needs):
    s = config.load()
    s.research.tools = ["web_search"]
    s.research.max_turns = 1
    config.save(s)
    llm = FakeLLM(monkeypatch, [search(), final("never reached")])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    assert {t["function"]["name"] for t in llm.requests[0]["tools"]} == {"web_search", "pmjc_result_get"}
    assert "turn limit" in out["summary"] and len(llm.requests) == 1


# --- the executor rule -------------------------------------------------------------------------------------
async def test_quarantined_caller_that_becomes_sensitive_is_denied_egress_again():
    sub = research.new_caller(parent())
    await executor.call_tool(sub, "web_search", {"queries": ["a"]})
    await executor.call_tool(sub, "web_fetch", {"urls": [A]})  # allowed: untrusted but not sensitive
    assert sub.untrusted and not sub.sensitive
    await executor.call_tool(sub, "priv_notes", {})  # a private_output tool
    assert sub.sensitive
    with pytest.raises(executor.Denied):
        await executor.call_tool(sub, "web_fetch", {"urls": [B]})
    assert denied_rows()[-1]["detail"] == "egress after untrusted content"


async def test_ordinary_caller_still_denied_after_untrusted():
    c = parent()
    await executor.call_tool(c, "web_search", {"queries": ["a"]})
    with pytest.raises(executor.Denied):
        await executor.call_tool(c, "web_fetch", {"urls": [A]})


def test_quarantined_caller_starts_clean():
    sub = research.new_caller(parent(name="x"))
    assert sub.name == "research:x" and sub.quarantine and not sub.sensitive and not sub.untrusted
    assert sub.scopes == {"sources"} and not sub.auto_approve and sub.thread_id is None


def test_only_the_research_tool_creates_quarantined_callers():
    src = Path(research.__file__).parent
    hits = {}
    for f in src.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        found = re.findall(r"quarantine\s*=(?!=)|\"quarantine\"|'quarantine'|setattr\([^)]*quarantine", text)
        if found:
            hits[f.name] = found
    assert set(hits) == {"research.py"}, hits
    assert len(re.findall(r"quarantine=True\)", (src / "research.py").read_text(encoding="utf-8"))) == 1


def test_quarantine_not_settable_from_job_watch_or_api(tmp_path):
    from pmjc import jobs, watches
    assert "quarantine" not in jobs.Job.model_fields
    job = jobs.Job.model_validate({"name": "j", "prompt": "p", "quarantine": True, "scopes": ["sources"]})
    assert not hasattr(job, "quarantine")
    w = {"id": "w1", "scopes": ["sources"], "spec": {"quarantine": True, "type": "time"}, "quarantine": True}
    assert watches.WatchManager._caller(None, w).quarantine is False
    c = auth.verify(auth.create_token("t", ["sources", "admin"]))
    assert c.quarantine is False
    assert auth.local().quarantine is False and auth.admin().quarantine is False
    assert "quarantine" not in config.Settings.model_fields


# --- what the parent gets ---------------------------------------------------------------------------------
async def test_parent_becomes_tainted_not_sensitive(monkeypatch, needs):
    FakeLLM(monkeypatch, [final("ok")])
    p = parent()
    assert not p.untrusted and not p.sensitive
    await executor.call_tool(p, "pmjc_research", {"question": "q"})
    assert p.untrusted and not p.sensitive
    # tainted: no second research, no web call, no act
    with pytest.raises(executor.Denied):
        await executor.call_tool(p, "pmjc_research", {"question": "again"})


async def test_sensitive_parent_stays_sensitive(monkeypatch, needs):
    FakeLLM(monkeypatch, [final("ok")])
    p = parent(sensitive=True)
    await executor.call_tool(p, "pmjc_research", {"question": "q"})
    assert p.untrusted and p.sensitive


async def test_research_denied_from_tainted_parent(monkeypatch, needs):
    llm = FakeLLM(monkeypatch, [final("never")])
    with pytest.raises(executor.Denied):
        await executor.call_tool(parent(untrusted=True), "pmjc_research", {"question": "q"})
    assert not llm.requests


async def test_invented_urls_are_dropped(monkeypatch, needs):
    fake = "https://invented.example.com/made-up"
    FakeLLM(monkeypatch, [search(), fetch(A), final(f"See {A} and {fake} and {B}.")])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    assert out["sources"] == [A, B]  # fetched first, then cited URLs that a tool result contained
    assert fake not in out["summary"] and "hxxps://invented[.]example[.]com/made-up" in out["summary"]


async def test_question_cap(monkeypatch, needs):
    llm = FakeLLM(monkeypatch, [final("ok")])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q" * 1000})
    assert out["question"] == "q" * 300 and "question_truncated" in out
    assert "q" * 301 not in llm.requests[0]["messages"][1]["content"]
    assert db.q("SELECT detail FROM audit WHERE status='question'")[0]["detail"] == "q" * 300


async def test_summary_cap(monkeypatch, needs):
    FakeLLM(monkeypatch, [final("s" * 9000)])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    assert len(out["summary"]) == 2400 and out["summary"].endswith("...")
    s = config.load()
    s.research.max_chars = 100
    config.save(s)
    FakeLLM(monkeypatch, [final("s" * 9000)])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    assert len(out["summary"]) == 100


async def test_max_sources_caps_the_list(monkeypatch, needs):
    FakeLLM(monkeypatch, [fetch(A, B), final("ok")])
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q", "max_sources": 1})
    assert out["sources"] == [A]


# --- routing, thread ------------------------------------------------------------------------------------
async def test_private_parent_thread_gives_private_sub_run(monkeypatch, needs):
    FakeLLM(monkeypatch, [final("a"), final("b")])
    p = parent()
    p.thread_id = memory.thread_new("secret plans", private=True)
    await executor.call_tool(p, "pmjc_research", {"question": "q"})
    assert needs[-1].private is True
    p2 = parent()
    p2.thread_id = memory.thread_new("open topic")
    await executor.call_tool(p2, "pmjc_research", {"question": "q"})
    assert needs[-1].private is False


async def test_research_is_noted_in_the_parent_thread_as_untrusted(monkeypatch, needs):
    FakeLLM(monkeypatch, [fetch(A), final(f"found it at {A}")])
    p = parent()
    p.thread_id = memory.thread_new("t")
    await executor.call_tool(p, "pmjc_research", {"question": "where?"})
    items = db.q("SELECT kind, trust, text, author FROM thread_items WHERE thread_id=? AND kind='research'", p.thread_id)
    assert len(items) == 1 and items[0]["trust"] == "untrusted" and items[0]["author"] == "research:agent"
    assert "where?" in items[0]["text"] and A in items[0]["text"] and "found it" in items[0]["text"]


async def test_no_tools_available_is_an_error(monkeypatch, needs):
    s = config.load()
    s.research.tools = []
    config.save(s)
    out = await executor.call_tool(parent(), "pmjc_research", {"question": "q"})
    assert "no research tools" in out["error"]


def test_tool_flags():
    t = registry.get().tool("pmjc_research")
    assert (t.scope, t.untrusted_output, t.egress, t.private_output, t.writes_state) == ("read", True, True, False, False)
