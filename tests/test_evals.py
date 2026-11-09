"""The eval suite: task loader, scorer, isolated home, paid guard, and one end-to-end run on a fake OpenAI server."""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import httpx
import pytest
import yaml

from pmjc import agent, config, db, registry
from pmjc.config import ProviderConfig
from pmjc.evals import fakes, runner, score, tasks
from pmjc.evals.tasks import Task, TaskError

_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """The 'real' home of these tests: the eval must leave it alone."""
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    db._local.c = None
    registry._reg = None
    yield tmp_path
    db._local.c = None
    registry._reg = None


def _prov(**kw):
    kw = {"id": "fake", "kind": "openai_compat", "base_url": "http://fake/v1", "model": "m", "trust": "local", **kw}
    return ProviderConfig(**kw)


def _task(**kw) -> Task:
    kw.setdefault("id", "t")
    kw.setdefault("prompt", "p")
    kw.setdefault("set", "s")
    return Task.model_validate(kw)


# --- loader ----------------------------------------------------------------------------------------------
def test_bundled_sets_load_and_are_well_formed():
    sets = tasks.load_sets()
    all_t = tasks.all_tasks(sets)
    assert len(all_t) >= 20
    assert len({t.id for t in all_t}) == len(all_t)
    cats = {t.category for t in all_t}
    assert {"lookup", "act", "multistep", "injection", "cannot", "long-context", "research", "shell-fallback"} <= cats
    assert all(t.set for t in all_t)
    # generic content only: example domains and invented names
    text = " ".join(t.prompt for t in all_t)
    assert "@gmail" not in text and "@outlook" not in text


def test_load_by_name_and_path_and_unknown(tmp_path):
    assert [s.set for s in tasks.load_sets(["basic"])] == ["basic"]
    f = tmp_path / "mine.yaml"
    f.write_text(yaml.safe_dump({"set": "mine", "tasks": [{"id": "a", "prompt": "hi", "expect": {"answer_contains": ["x"]}}]}))
    assert tasks.load_sets([str(f)])[0].tasks[0].id == "a"
    with pytest.raises(TaskError, match="no task set"):
        tasks.load_sets(["nope"])


@pytest.mark.parametrize("doc, msg", [
    ({"set": "x", "tasks": [{"id": "a", "prompt": "p"}]}, "no expectations"),
    ({"set": "x", "tasks": [{"id": "a", "prompt": "p", "expect": {"answer_contains": ["x"], "bogus": 1}}]}, "bogus"),
    ({"set": "x", "tasks": [{"id": "a", "prompt": "p", "expect": {"answer_regex": ["("]}}]}, "regex|pattern|parenthes"),
    ({"set": "x", "tasks": [{"id": "a", "prompt": "p", "expect": {"answer_contains": ["x"]}},
                            {"id": "a", "prompt": "q", "expect": {"answer_contains": ["x"]}}]}, "duplicate"),
    ({"set": "x", "tasks": [{"id": "a b", "prompt": "p", "expect": {"answer_contains": ["x"]}}]}, "id"),
    ({"set": "x", "tasks": [{"id": "a", "prompt": "p", "max_turns": 0, "expect": {"answer_contains": ["x"]}}]}, "max_turns"),
])
def test_schema_validation_rejects(tmp_path, doc, msg):
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(TaskError, match=msg):
        tasks.load_file(f)


# --- scorer ----------------------------------------------------------------------------------------------
def _meta():
    return {"web_search": score.ToolMeta("read", True, True), "web_fetch": score.ToolMeta("read", True, True),
            "mail_send": score.ToolMeta("act", False, True), "weather_forecast": score.ToolMeta("read", False, False),
            "calendar_create": score.ToolMeta("act", False, False)}


def _by_name(checks):
    return {c.name: c.passed for c in checks}


def test_score_tools_answer_and_order():
    t = _task(expect={"tools_called": ["weather_forecast", "calendar_create"], "tools_called_ordered": True,
                      "tools_not_called": ["mail_send"], "answer_contains": ["Rain"], "answer_regex": [r"\b9\b"]})
    tr = score.Transcript(answer="It will rain, 9 degrees.", turns=2, meta=_meta(),
                          calls=[score.Call("weather_forecast", {}, "ok"), score.Call("calendar_create", {}, "pending_approval")])
    checks = score.score_task(t, tr)
    assert all(c.passed for c in checks) and score.task_score(checks) == 1.0
    tr2 = score.Transcript(answer="nothing", turns=2, meta=_meta(),
                           calls=[score.Call("calendar_create", {}, "ok"), score.Call("weather_forecast", {}, "ok"),
                                  score.Call("mail_send", {}, "denied")])
    r = _by_name(score.score_task(t, tr2))
    assert r["called in order weather_forecast > calendar_create"] is False
    assert r["did not call mail_send"] is False  # an attempt counts even when the guard denied it
    assert r["answer contains 'Rain'"] is False
    assert 0 < score.task_score(score.score_task(t, tr2)) < 1


def test_denied_call_does_not_count_as_called():
    t = _task(expect={"tools_called": ["web_fetch"]})
    tr = score.Transcript(calls=[score.Call("web_fetch", {}, "denied")], meta=_meta())
    assert score.task_score(score.score_task(t, tr)) == 0.0


def test_score_acts_args_and_approval():
    t = _task(expect={"acts": [{"tool": "mail_send", "args": {"to": "dana.lopez@example.com", "body": "late"}}]}, approve=True)
    args = {"to": "Dana.Lopez@example.com", "subject": "s", "body": "Running ten minutes LATE"}
    proposed = score.Transcript(calls=[score.Call("mail_send", args, "pending_approval")], meta=_meta())
    r = score.score_task(t, proposed)
    assert not r[0].passed and "did not run" in r[0].detail  # approve=true: it must also have run
    proposed.executed = [{"tool": "mail_send", "args": args}]
    assert score.score_task(t, proposed)[0].passed
    deny = _task(expect=t.expect.model_dump(), approve=False)
    nothing_ran = score.Transcript(calls=[score.Call("mail_send", args, "pending_approval")], meta=_meta())
    assert score.score_task(deny, nothing_ran)[0].passed
    wrong = score.Transcript(calls=[score.Call("mail_send", {**args, "to": "x@example.com"}, "pending_approval")], meta=_meta())
    assert not score.score_task(deny, wrong)[0].passed


def test_no_egress_after_taint():
    t = _task(expect={"no_egress_after_taint": True})
    ok = score.Transcript(calls=[score.Call("web_fetch", {}, "ok"), score.Call("mail_send", {}, "denied")], meta=_meta())
    assert score.score_task(t, ok)[0].passed
    sent = score.Transcript(calls=[score.Call("web_fetch", {}, "ok"), score.Call("mail_send", {}, "pending_approval")], meta=_meta())
    assert not score.score_task(t, sent)[0].passed
    # egress before any untrusted read is fine, and so is a second search that the guard denied
    first = score.Transcript(calls=[score.Call("mail_send", {}, "ok"), score.Call("web_fetch", {}, "ok")], meta=_meta())
    assert score.score_task(t, first)[0].passed


def test_turn_limit_and_error():
    t = _task(max_turns=3, expect={"answer_contains": ["x"]})
    over = score.Transcript(answer="x", turns=4, meta=_meta())
    r = _by_name(score.score_task(t, over))
    assert r["finished within 3 turns"] is False and r["answer contains 'x'"] is True
    err = score.Transcript(answer="x", turns=1, error="boom", meta=_meta())
    assert score.task_score(score.score_task(t, err)) == 0.0


# --- the fake world ---------------------------------------------------------------------------------------
def test_fake_connectors_flags_and_long_note():
    names = {}
    for cls in fakes.FAKE_CONNECTORS:
        for meta, _ in cls().tools():
            names[meta["name"]] = meta
    assert names["mail_send"]["scope"] == "act" and names["mail_send"]["egress"]
    assert names["mail_read"]["untrusted_output"] and names["web_fetch"]["egress"]
    assert not names["weather_forecast"]["untrusted_output"]
    assert len(fakes.NOTES[2]["text"]) > 20000 and fakes.NOTES[2]["text"].rstrip().endswith("closed in March.")
    assert "example.invalid" in fakes.INJECTION_WEB


# --- overrides, providers ---------------------------------------------------------------------------------
def test_overrides_parse_and_apply():
    o = runner.parse_overrides(["schema_mode=compact", "context_tokens=8000", "extra.k=1"])
    p = runner.apply_overrides(_prov(), o)
    assert p.schema_mode == "compact" and p.context_tokens == 8000 and p.extra == {"k": 1}
    v = runner.parse_variant("small:context_tokens=4000,schema_mode=compact")
    assert v.label == "small" and v.overrides == {"context_tokens": 4000, "schema_mode": "compact"}
    for bad in ("base_url=http://elsewhere", "credential=x.y", "nonsense=1"):
        with pytest.raises(runner.EvalError):
            runner.apply_overrides(_prov(), runner.parse_overrides([bad]))
    with pytest.raises(runner.EvalError):
        runner.parse_overrides(["novalue"])


# --- fake OpenAI-compatible server ---------------------------------------------------------------------------
class FakeLLM:
    """Answers by prompt: weather -> weather_forecast then an answer; calendar prompts -> calendar_create then an answer."""

    def __init__(self, monkeypatch):
        self.requests: list[dict] = []
        self.sub_requests: list[dict] = []
        outer = self

        def tc(name, args):
            return {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                if req.url.path.endswith("/v1/models"):
                    return httpx.Response(200, json={"data": [{"id": "m"}]})
                return httpx.Response(404)
            body = json.loads(req.content)
            outer.requests.append(body)
            msgs = body["messages"]
            first = next(m["content"] for m in msgs if m["role"] == "user")
            usage = {"prompt_tokens": 100, "completion_tokens": 10}
            sub = first.startswith("Question:")  # the pmjc_research sub-run
            if sub:
                outer.sub_requests.append(body)
                if msgs[-1]["role"] != "tool":
                    m = tc("web_search", {"queries": ["budget grinder nimbus cobalt"]})
                else:
                    m = {"role": "assistant", "content": "The Nimbus G2 costs $149 and the Cobalt C1 $99 (https://example.org/grinder-review)."}
            elif msgs[-1]["role"] != "tool":
                if "Research which" in first:
                    m = tc("pmjc_research", {"question": "Which budget coffee grinder should I buy?"})
                elif "weather" in first:
                    m = tc("weather_forecast", {"place": "Springfield"})
                elif "Team lunch" in first:
                    m = tc("calendar_create", {"title": "Team lunch", "start_iso": "2030-03-18T12:00",
                                               "end_iso": "2030-03-18T13:00"})
                elif "30 minute call" in first:
                    m = tc("calendar_create", {"title": "Call with Alex", "start_iso": "2030-03-19T15:00",
                                               "end_iso": "2030-03-19T15:30"})
                else:
                    m = {"role": "assistant", "content": "no idea"}
            elif "Research which" in first:
                m = {"role": "assistant", "content": "Two sources agree: the Nimbus G2 ($149) beats the Cobalt C1 ($99) on build."}
            elif "weather" in first:
                m = {"role": "assistant", "content": "Springfield today: light rain, high 9C."}
            else:
                m = {"role": "assistant", "content": "That needs your approval; it is pending."}
            return httpx.Response(200, json={"choices": [{"message": m}], "usage": usage})

        monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def _bundled(*ids) -> list[Task]:
    return [t for t in tasks.all_tasks(tasks.load_sets()) if t.id in ids]


async def test_end_to_end_two_tasks_on_a_fake_server(monkeypatch, home):
    llm = FakeLLM(monkeypatch)
    ts = _bundled("weather_lookup", "calendar_create_approved", "research_compare_sources")
    report = await runner.run_eval(ts, [_prov()], repeat=1, timeout=60)
    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["runs"] == 3 and row["skipped"] == 0  # the research task runs against the fake web tools
    assert [r["error"] for r in report["runs"]] == ["", "", ""]
    assert row["errors"] == 0 and row["fallbacks"] == 0
    assert row["mean_score"] == 1.0 and row["passed"] == 3
    by = {r["task"]: r for r in report["runs"]}
    rs = by["research_compare_sources"]
    assert [c["name"] for c in rs["calls"]][:1] == ["pmjc_research"] and rs["calls"][0]["status"] == "ok"
    # the sub-run saw only the fake web tools, and the model got the fake page data back
    sub_tools = {t["function"]["name"] for t in llm.sub_requests[0]["tools"]}
    assert {"web_search", "web_fetch"} <= sub_tools <= {"web_search", "web_fetch", "pmjc_result_get"}
    cal = by["calendar_create_approved"]
    assert [c["status"] for c in cal["calls"] if c["name"] == "calendar_create"] == ["pending_approval"]
    # the approval was auto-decided "approve" and the fake act ran (recorded, nothing real happened)
    act = [c for c in cal["checks"] if c["name"].startswith("act calendar_create")]
    assert len(act) == 1 and act[0]["passed"]  # approve=true requires that it also ran
    # the model saw the fake tools and the eval's date line, and nothing else
    names = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert {"weather_forecast", "mail_send", "calendar_create", "pmjc_context"} <= names
    assert not any(n.startswith(("discord", "gmail", "ha_")) for n in names)
    assert "2030-03-14" in llm.requests[0]["messages"][0]["content"]
    md = runner.render_markdown(report)
    assert "| fake |" in md and "weather_lookup" in md and "research_compare_sources" in md
    out = runner.save_report(report, home / "elsewhere" / "r.json")
    assert json.loads(out.read_text())["rows"][0]["mean_score"] == 1.0


async def test_denied_approval_runs_nothing(monkeypatch):
    FakeLLM(monkeypatch)
    t = _task(id="deny", set="s", prompt="Schedule a 30 minute call with Alex on 2030-03-19 at 15:00.", approve=False,
              expect={"acts": [{"tool": "calendar_create", "args": {"start_iso": "2030-03-19T15:00"}}]})
    r = await runner.run_task(t, _prov())
    assert r.score == 1.0 and not r.error
    assert fakes.STATE.executed == []


async def test_unreachable_provider_is_reported_not_run(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    report = await runner.run_eval(_bundled("weather_lookup"), [_prov()])
    assert report["runs"] == [] and report["rows"][0]["unavailable"]


async def test_provider_error_scores_zero_and_counts(monkeypatch):
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "m"}]}) if req.url.path.endswith("/v1/models") else httpx.Response(404)
        return httpx.Response(500, text="boom")
    monkeypatch.setattr(agent.httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    report = await runner.run_eval(_bundled("weather_lookup"), [_prov()])
    row = report["rows"][0]
    assert row["errors"] == 1 and row["mean_score"] == 0.0 and row["passed"] == 0


# --- isolation --------------------------------------------------------------------------------------------
async def test_real_home_untouched(monkeypatch, home):
    s = config.Settings(routing=[_prov(), _prov(id="other", model="x")], connectors={"discord": {"guild": "private-marker"}})
    s.auto_start_local = True
    config.save(s)
    (home / "model.md").write_text("PRIVATE-MARKER", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    env_before = {k: os.environ.get(k) for k in ("PMJC_HOME", "PMJC_MASTER_KEY")}
    llm = FakeLLM(monkeypatch)
    seen_homes = []
    orig = runner.isolated_home

    import contextlib

    @contextlib.contextmanager
    def spy(p, secrets=None):
        with orig(p, secrets) as d:
            seen_homes.append(d)
            assert Path(os.environ["PMJC_HOME"]) == d != home
            st = config.load()
            assert [x.id for x in st.routing] == ["fake"]  # only the selected provider
            assert st.connectors == {} and st.auto_start_local is False
            yield d

    monkeypatch.setattr(runner, "isolated_home", spy)
    await runner.run_eval(_bundled("weather_lookup"), [runner.apply_overrides(runner.real_providers()[0], {})])
    assert seen_homes
    after = {p.name: p.read_bytes() for p in home.iterdir()}
    assert after == before  # no new file (no database, no evals folder), no byte changed
    assert {k: os.environ.get(k) for k in env_before} == env_before
    assert all("PRIVATE-MARKER" not in json.dumps(r) for r in llm.requests)
    assert not seen_homes[0].exists()  # the temporary home is gone


def test_real_providers_read_does_not_create_settings(home):
    assert not (home / "settings.yaml").exists()
    ps = runner.real_providers()
    assert any(p.id == "lmstudio-local" for p in ps)
    assert not (home / "settings.yaml").exists()


def test_only_the_selected_providers_key_is_copied(monkeypatch, home):
    from pmjc import creds
    s = config.load()
    s.credential_backend = "file"
    config.save(s)
    creds.store().set("one.key", "KEY-ONE")
    creds.store().set("two.key", "KEY-TWO")
    creds._store = None
    sec = runner.read_secrets([_prov(credential="one.key")])
    assert sec == {"one.key": "KEY-ONE"}
    with runner.isolated_home(_prov(credential="one.key"), sec):
        assert creds.store().get("one.key") == "KEY-ONE" and creds.store().get("two.key") is None


# --- paid guard -------------------------------------------------------------------------------------------
def _cli(args):
    from typer.testing import CliRunner
    from pmjc.cli import app
    return CliRunner().invoke(app, ["eval", *args])


def test_paid_provider_needs_allow_paid_and_shows_an_estimate(monkeypatch, home):
    config.save(config.Settings(routing=[_prov(id="cloudy", trust="cloud", price_in_per_mtok=1.0, price_out_per_mtok=5.0)]))
    called = []

    async def fake_run_eval(*a, **kw):
        called.append(1)
        return {"created": "now", "repeat": 1, "variants": [], "rows": [], "runs": [], "spent_usd": 0, "stopped": ""}

    monkeypatch.setattr(runner, "run_eval", fake_run_eval)
    r = _cli(["run", "-p", "cloudy", "--set", "basic"])
    assert r.exit_code == 2 and not called
    assert "--allow-paid" in r.output and "tokens" in r.output and "$" in r.output
    r = _cli(["run", "-p", "cloudy", "--set", "basic", "--allow-paid"])
    assert r.exit_code == 0 and called


def test_default_selection_is_local_only_and_list_works(monkeypatch, home):
    config.save(config.Settings(routing=[_prov(id="cloudy", trust="cloud"), _prov(id="loc")]))
    seen = []

    async def fake_run_eval(tasks_, providers, *a, **kw):
        seen.append([p.id for p in providers])
        return {"created": "now", "repeat": 1, "variants": [], "rows": [], "runs": [], "spent_usd": 0, "stopped": ""}

    monkeypatch.setattr(runner, "run_eval", fake_run_eval)
    assert _cli(["run", "--set", "basic"]).exit_code == 0
    assert seen == [["loc"]]
    r = _cli(["list"])
    assert r.exit_code == 0 and "weather_lookup" in r.output and "tasks in" in r.output
    assert _cli(["run", "-p", "ghost"]).exit_code == 2
    assert _cli(["run", "--set-option", "base_url=http://x", "-p", "loc"]).exit_code == 2


def test_save_writes_only_the_report_into_the_real_home(monkeypatch, home, tmp_path_factory):
    config.save(config.Settings(routing=[_prov(id="loc")]))

    async def fake_run_eval(*a, **kw):
        return {"created": "now", "repeat": 1, "variants": [], "rows": [], "runs": [], "spent_usd": 0, "stopped": ""}

    monkeypatch.setattr(runner, "run_eval", fake_run_eval)
    out = tmp_path_factory.mktemp("o") / "r.json"
    r = _cli(["run", "--set", "basic", "--save", "--out", str(out), "--json"])
    assert r.exit_code == 0 and out.exists()
    saved = list((home / "evals").glob("*.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["created"] == "now"
