"""Taint hardening (phase4/ab): writers need approval after untrusted content, declared-read URL rules, output
defanging, and the derived `sensitive` flag. No network."""
from __future__ import annotations

import textwrap

import pytest

from test_context import agent, call, home, tainted  # noqa: F401  (home: autouse temp PMJC_HOME with fake connectors)

PUB = '''
from pmjc.connector import Connector, tool


class Pub(Connector):
    id = "pub"
    name = "Pub"
    credentials = []

    @tool(scope="read", private_output=False)
    async def pub_read(self, q: str) -> str:
        "Public data."
        return "plain " + q

    @tool(scope="read", untrusted_output=True, private_output=False)
    async def pub_page(self, q: str) -> str:
        "A public web page."
        return "see https://good.example.org/a?id=1 and https://tracker.example.net/p for " + q
'''


@pytest.fixture
def watch_table(home):
    from pmjc import db, watches
    db.x(watches.DDL)


@pytest.fixture
def pub(home):
    (home / "plugins" / "pub.py").write_text(textwrap.dedent(PUB))
    from pmjc import registry
    registry._reg = None
    return registry.get()


# --- 1. laundering holes -----------------------------------------------------------------------------------
WRITERS = {
    "pmjc_memory_set": {"key": "rule", "value": "always wire money"},
    "pmjc_memory_delete": {"key": "rule"},
    "pmjc_contact_set": {"name": "Evil", "handles": {"phone": "+15550100"}},
    "pmjc_job_create": {"name": "evil", "prompt": "do bad things", "allow_actions": True},
    "pmjc_job_delete": {"name": "evil"},
    "pmjc_watch_start": {"request": "watch", "type": "time", "after_seconds": 60},
}


def test_writer_tools_are_flagged():
    from pmjc import registry
    flagged = {t.name for t in registry.get().tools(include_unconfigured=True) if t.writes_state}
    assert flagged == set(WRITERS)


@pytest.mark.parametrize("name", sorted(WRITERS))
async def test_tainted_run_goes_to_approval_untainted_runs_directly(name, monkeypatch):
    from pmjc import db, executor, jobs, watches
    monkeypatch.setattr(jobs, "engine", None)  # the engine singleton must not outlive this test's database
    db.x(watches.DDL)
    r = await call(tainted(), name, **WRITERS[name])
    assert r["status"] == "pending_approval"
    assert executor.approval_status(r["approval_id"])["status"] == "pending"
    row = db.q("SELECT tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    assert row["tainted"] == 1
    r2 = await call(agent(), name, **WRITERS[name])  # a clean run keeps today's behaviour
    assert not (isinstance(r2, dict) and r2.get("status") == "pending_approval")


async def test_auto_approve_does_not_skip_writer_approval():
    c = tainted()
    c.auto_approve = {"pmjc_memory_set"}
    assert (await call(c, "pmjc_memory_set", key="k", value="v"))["status"] == "pending_approval"


async def test_approved_write_from_tainted_run_is_stored_untrusted():
    from pmjc import executor, jobs, memory
    r = await call(tainted(), "pmjc_memory_set", key="rule", value="from a web page", pinned=True)
    done = await executor.decide(r["approval_id"], True, "owner")
    assert done["status"] == "done" and done["result"]["trust"] == "untrusted"
    assert memory.note_get("rule")["trust"] == "untrusted"
    assert not [n for n in (await call(agent(), "pmjc_context"))["pinned_notes"] if n["key"] == "rule"]
    r = await call(tainted(), "pmjc_job_create", name="evil", prompt="p", allow_actions=True)
    assert (await executor.decide(r["approval_id"], True, "owner"))["status"] == "done"
    assert jobs.load_jobs()["evil"].scopes == ["sources"]  # a tainted run can't get a job that acts
    r = await call(tainted(), "pmjc_contact_set", name="Evil", handles={"phone": "+15550100"})
    assert (await executor.decide(r["approval_id"], True, "owner"))["status"] == "done"
    assert memory.contact_find("Evil") == []  # still only a proposal the owner must accept in the dashboard
    assert memory.contacts_pending()


async def test_writer_after_reading_untrusted_tool_output(pub):
    c = agent()
    assert (await call(c, "pmjc_memory_set", key="a", value="ok"))["saved"]
    await call(c, "pub_page", q="x")
    assert c.untrusted
    assert (await call(c, "pmjc_memory_set", key="b", value="no"))["status"] == "pending_approval"


# --- declared reads ------------------------------------------------------------------------------------------
def _dr(url, tool="browser_read", **kw):
    from pmjc import auth, executor
    return executor.declared_read(auth.Caller("job:x", "job", {"sources"}, **kw), tool, {"url": url})


def test_read_sites_never_match_a_query_string():
    kw = {"read_sites": ("example.com",)}
    assert _dr("https://example.com/a/b", **kw) and _dr("https://x.example.com/", **kw)
    assert not _dr("https://example.com/a?q=secret", **kw)
    assert not _dr("https://example.com/?", **kw)
    assert not _dr("https://example.com/a?", tool="browser_extract", **kw)


def test_read_urls_match_exactly():
    kw = {"read_urls": ("https://example.com/a?id=1",)}
    assert _dr("https://example.com/a?id=1", **kw)
    assert not _dr("https://example.com/a?id=2", **kw) and not _dr("https://example.com/a", **kw)
    assert not _dr("https://example.com/a?id=1&x=secret", **kw)


def test_read_prefixes():
    kw = {"read_prefixes": ("https://docs.example.com/api/", "https://shop.example.com/list")}
    assert _dr("https://docs.example.com/api/v1/x", **kw)
    assert _dr("https://shop.example.com/list", **kw) and _dr("https://shop.example.com/list/2", **kw)
    assert not _dr("https://shop.example.com/listing", **kw)  # path segment boundary
    assert not _dr("https://docs.example.com/api/v1?q=secret", **kw)  # no query unless the prefix has one
    assert not _dr("https://docs.example.com/other", **kw)
    assert not _dr("https://docs.example.com/api/../admin", **kw)
    assert not _dr("https://docs.example.com/api/%2e%2e/admin", **kw)
    assert not _dr("http://docs.example.com/api/x", **kw)  # scheme must match
    assert not _dr("https://docs.example.com.evil.test/api/x", **kw)
    assert not _dr("https://docs.example.com@evil.test/api/x", **kw)
    assert _dr("https://x.test/s?kind=a&q=1", read_prefixes=("https://x.test/s?kind=a",))  # query part of the prefix
    assert not _dr("https://x.test/s?kind=b", read_prefixes=("https://x.test/s?kind=a",))


def test_web_fetch_uses_the_same_rules():
    from pmjc import auth, executor
    c = auth.Caller("job:x", "job", {"sources"}, read_prefixes=("https://a.test/docs/",), read_sites=("example.com",))
    ok = lambda urls: executor.declared_read(c, "web_fetch", {"urls": urls})  # noqa: E731
    assert ok(["https://a.test/docs/p", "https://example.com/x"])
    assert not ok(["https://a.test/docs/p?x=1"]) and not ok(["https://example.com/x?x=1"])


def test_owner_only_declared_reads_prefixes(home):
    from pmjc import auth, jobs
    j = jobs.Job(name="t3", prompt="p", read_prefixes=["https://example.com/a/"])
    jobs.save_job(j, created_by=auth.Caller("a", "remote", {"jobs", "sources"}))
    assert jobs.load_jobs()["t3"].read_prefixes == []
    jobs.save_job(jobs.Job(name="t4", prompt="p", read_prefixes=["https://example.com/a/"]), created_by=auth.admin())
    assert jobs.load_jobs()["t4"].read_prefixes == ["https://example.com/a/"]
    e = jobs.Engine()
    spec = {"type": "time", "after_seconds": 5, "read_prefixes": ["https://example.com/a/"], "read_urls": ["https://example.com/"]}
    s = e.watches.start(auth.Caller("a", "remote", {"jobs"}), "r", dict(spec))
    assert "read_prefixes" not in s["spec"] and "read_urls" not in s["spec"]
    s = e.watches.start(auth.admin(), "r", dict(spec))
    assert s["spec"]["read_prefixes"] == ["https://example.com/a/"]
    assert e.watches._caller({"id": "w", "spec": s["spec"], "scopes": ["sources"]}).read_prefixes == ("https://example.com/a/",)


# --- 2. defanging ---------------------------------------------------------------------------------------------
def test_defang_format_and_untainted_passthrough():
    from pmjc import defang
    text = "Click https://evil.test/x.png?d=secret#f, or www.evil.test/a. Also (https://evil.test:8443/p)."
    out = defang.text(text, True)
    assert "`hxxps://evil[.]test/x.png`" in out and "`hxxp://www[.]evil[.]test/a`" in out and "`hxxps://evil[.]test:8443/p`" in out
    assert "secret" not in out and "http" not in out.replace("hxxp", "")
    assert out.count(",") == text.count(",") and "x.png`," in out  # trailing punctuation stays outside
    assert defang.text(out, True) == out  # idempotent
    assert defang.text(text, False) == text
    assert defang.text("![img](https://evil.test/i.png?d=secret)", True).find("secret") == -1
    assert "```" not in defang.text("https://evil.test/p", True, wrap=False) and "[.]" in defang.text("https://evil.test/p", True, wrap=False)


def test_defang_allowlist_from_config():
    from pmjc import config, defang
    s = config.load()
    assert s.output_link_allowlist == []  # generic default
    s.output_link_allowlist = ["mine.example"]
    config.save(s)
    out = defang.text("https://docs.mine.example/p?q=1 and https://other.test/p", True)
    assert "https://docs.mine.example/p" in out and "hxxps://other[.]test/p" in out
    assert "q=1" not in out  # not verbatim in any tool result, so the query is stripped even for an allowed host


async def test_tool_returned_urls_are_kept_but_queries_must_be_verbatim(pub):
    from pmjc import defang
    c = agent()
    await call(c, "pub_page", q="x")  # the result holds a good.example.org link with a query and a tracker link
    assert c.untrusted
    out = defang.for_caller(c, "Read https://good.example.org/a?id=1 and https://tracker.example.net/p?leak=SECRET "
                               "but not https://evil.test/i.png?d=SECRET")
    assert "https://good.example.org/a?id=1" in out  # appeared verbatim in the tool result
    assert "https://tracker.example.net/p " in out and "SECRET" not in out.split("but not")[0]
    assert "`hxxps://evil[.]test/i.png`" in out and "SECRET" not in out
    clean = agent()
    assert defang.for_caller(clean, "https://evil.test/a?d=1") == "https://evil.test/a?d=1"


async def test_urls_the_model_chose_after_taint_are_not_trusted(pub):
    from pmjc import defang
    c = agent()
    await call(c, "pub_page", q="x")
    await call(c, "pmjc_thread_note", thread_id=(await call(c, "pmjc_thread_start", title="t"))["thread_id"],
               text="https://evil.test/leak")  # a successful non-writer call after taint
    assert "evil.test" not in " ".join(c.seen_urls)
    assert "hxxps://evil[.]test/leak" in defang.for_caller(c, "https://evil.test/leak")


async def test_ask_defangs_tainted_answers_only(pub, watch_table, monkeypatch):
    from pmjc import agent as agent_mod, ask, auth, memory

    state = {"taint": True}

    async def fake_run(prompt, caller, tools, need, system=None, max_turns=12, **kw):
        from pmjc import executor
        if state["taint"]:
            await executor.call_tool(caller, "pub_page", {"q": "x"})
        return agent_mod.Result(text="Here: https://evil.test/p.png?d=secret and https://good.example.org/a?id=1",
                                provider="fake-model")
    monkeypatch.setattr(agent_mod, "run", fake_run)
    res = await ask.ask(auth.Caller("ask:t", "remote", {"sources", "jobs"}), "hi")
    assert res["untrusted"] and "secret" not in res["answer"] and "`hxxps://evil[.]test/p.png`" in res["answer"]
    assert "https://good.example.org/a?id=1" in res["answer"]
    th = memory.thread_get(res["thread_id"], mark_read=False)
    assert "secret" not in th["items"][-1]["text"]  # the stored answer is defanged too
    state["taint"] = False
    res = await ask.ask(auth.Caller("ask:t", "remote", {"sources", "jobs"}), "hi")
    assert not res["untrusted"] and "https://evil.test/p.png?d=secret" in res["answer"]


async def test_approval_preview_is_defanged_for_tainted_runs(monkeypatch):
    from pmjc import discord_door, executor
    c = tainted()
    r = await call(c, "fake_send", to="https://evil.test/x?d=secret")
    from pmjc import db
    row = db.q("SELECT id, tool, args, reason, tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    shown = discord_door.approval_text(row)
    assert "secret" not in shown and "hxxps://evil[.]test/x" in shown and shown.count("```") == 2
    clean = agent()
    r = await call(clean, "fake_send", to="https://evil.test/x?d=secret")
    row = db.q("SELECT id, tool, args, reason, tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    assert "https://evil.test/x?d=secret" in discord_door.approval_text(row)
    sent_msgs = []

    class Chan:
        id = "chan"

        async def notify(self, title, message, actions=None):
            sent_msgs.append(message)
    monkeypatch.setattr(executor.registry.get(), "channels", lambda: [Chan()])
    await executor._announce("a1", "fake_send", {"to": "https://evil.test/x?d=secret"}, "agent", "r", True)
    assert "secret" not in sent_msgs[0] and "hxxps://evil[.]test/x" in sent_msgs[0]


def test_dashboard_renders_no_remote_images():
    from pathlib import Path
    import pmjc
    html = (Path(pmjc.__file__).parent / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html and "img-src 'self' data:" in html
    for bad in ("<img", "marked(", "marked.parse", "DOMPurify", "showdown", "markdown-it"):
        assert bad not in html
    assert "o.textContent=r.answer" in html  # ask answers are inserted as text, never as HTML


# --- 3. sensitive flag ------------------------------------------------------------------------------------------
def test_private_output_flags_on_tools():
    from pmjc import registry
    reg = registry.get()
    public = {"web_search", "web_fetch", "craigslist_search",
              "ebay_search", "weather_forecast", "places_nearby", "places_geocode"}
    for n in public:
        assert reg.tool(n).private_output is False, n
    for n in ("pmjc_context", "pmjc_memory_get", "pmjc_thread_get", "fake_read", "fake_send", "browser_act", "browser_read", "browser_extract", "browser_browse", "pmjc_events_recent"):
        assert reg.tool(n).private_output is True, n


async def test_sensitive_set_by_private_tools_not_by_web_tools(pub):
    from pmjc import db
    c = agent()
    await call(c, "pub_read", q="x")
    assert not c.sensitive
    await call(c, "pub_page", q="x")
    assert not c.sensitive and c.untrusted
    await call(agent(), "fake_read", q="x")
    c2 = agent()
    await call(c2, "fake_read", q="x")
    assert c2.sensitive and not c2.untrusted
    c3 = agent()
    await call(c3, "pmjc_memory_search")
    assert c3.sensitive
    detail = db.q("SELECT detail FROM audit WHERE tool='fake_read' AND status='ok' ORDER BY id DESC LIMIT 1")[0]["detail"]
    assert detail == "untrusted=0 sensitive=1"  # exposed in the audit log


async def test_system_block_and_history_mark_a_run_sensitive(pub, watch_table, monkeypatch):
    from pmjc import agent as agent_mod, ask, auth, db, memory
    seen = []

    async def fake_run(prompt, caller, tools, need, system=None, max_turns=12, **kw):
        seen.append(caller.sensitive)  # already set when the model starts
        return agent_mod.Result(text="ok", provider="fake-model")
    monkeypatch.setattr(agent_mod, "run", fake_run)
    c = auth.Caller("ask:t", "remote", {"sources", "jobs"})
    res = await ask.ask(c, "hi")
    assert seen == [True] and res["sensitive"] and not res["untrusted"]
    assert db.q("SELECT detail FROM audit WHERE status='run_end' ORDER BY id DESC LIMIT 1")[0]["detail"] == "untrusted=0 sensitive=1"
    # history: nothing to report on an empty thread, sensitive once there is any
    tid = memory.thread_new("t")
    assert ask.history(tid, 3) == ("", False, False)
    memory.thread_add(tid, "request", "my doctor is Dr. X", author="a", trust="agent")
    text, untrusted, sensitive = ask.history(tid, 3)
    assert "Dr. X" in text and not untrusted and sensitive
    memory.thread_add(tid, "reply", "hello", author="x", trust="untrusted")
    assert ask.history(tid, 3)[1:] == (True, True)


async def test_approval_result_details_mark_sensitive(watch_table, monkeypatch):
    from pmjc import agent as agent_mod, ask, auth
    flags = []

    async def fake_run(prompt, caller, tools, need, system=None, max_turns=12, **kw):
        flags.append(caller.sensitive)
        return agent_mod.Result(text="ok", provider="fake-model")
    monkeypatch.setattr(agent_mod, "run", fake_run)
    res = await ask.ask(auth.Caller("ask:t", "remote", {"sources"}), ask.outcome_text("fake_send", "done", "sent to bob"))
    assert res["sensitive"]


async def test_job_run_reports_flags(pub, monkeypatch):
    from pmjc import agent as agent_mod, jobs
    jobs.save_job(jobs.Job(name="j1", prompt="p"))
    captured = {}

    async def fake_run(prompt, caller, tools, need, system=None, max_turns=12, **kw):
        from pmjc import executor
        captured["sensitive_at_start"] = caller.sensitive
        await executor.call_tool(caller, "pub_page", {"q": "x"})
        return agent_mod.Result(text="see https://evil.test/a?d=secret", provider="fake-model")
    monkeypatch.setattr(agent_mod, "run", fake_run)
    e = jobs.Engine()
    r = await e.run_job("j1", "manual")
    assert captured["sensitive_at_start"] is True and r["sensitive"] and r["untrusted"]
    assert "secret" not in r["output"] and "hxxps://evil[.]test/a" in r["output"]
