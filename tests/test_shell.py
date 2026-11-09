"""The approved shell-command fallback (pmjc_shell_run): gating, approval, limits, audit. Harmless commands only."""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "test-master-key")
    from pmjc import config, creds, db, memory, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    s = config.load()
    s.credential_backend = "file"
    s.shell_command_cwd = str(tmp_path)
    config.save(s)
    yield tmp_path
    db._local.c = None


def caller(name="agent", kind="remote", **kw):
    from pmjc import auth
    c = auth.Caller(name, kind, {"sources", "actions", "jobs"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


async def propose(c, **args):
    from pmjc import executor
    return await executor.call_tool(c, "pmjc_shell_run", args)


def audit(status):
    from pmjc import db
    return db.q("SELECT * FROM audit WHERE tool='pmjc_shell_run' AND status=?", status)


def set_cfg(**kw):
    from pmjc import config
    s = config.load()
    for k, v in kw.items():
        setattr(s, k, v)
    config.save(s)


def stored_args(aid):
    from pmjc import db
    return json.loads(db.q("SELECT args FROM approvals WHERE id=?", aid)[0]["args"])


def test_defaults_are_generic():
    from pmjc import config
    s = config.load()
    assert s.shell_command_proposers == ["owner"]
    assert s.shell_command_timeout_seconds == 30 and s.shell_command_output_cap_chars == 10000
    assert s.shell_command_max_timeout_seconds >= s.shell_command_timeout_seconds
    assert s.shell_command_max_output_chars >= s.shell_command_output_cap_chars


def test_tool_is_act_with_untrusted_output():
    from pmjc import registry
    t = registry.get().tool("pmjc_shell_run")
    assert t.scope == "act" and t.untrusted_output


async def test_owner_chat_proposal_waits_for_approval_and_shows_command(home):
    from pmjc import db, executor
    r = await propose(caller(), command="echo hello-pmjc")
    assert r["status"] == "pending_approval"
    assert db.q("SELECT status FROM approvals WHERE id=?", r["approval_id"])[0]["status"] == "pending"
    args = stored_args(r["approval_id"])
    assert args["command"] == "echo hello-pmjc"
    assert {"shell", "cwd", "timeout_seconds", "max_output_chars"} <= set(args)
    assert args["cwd"] == str(home) and args["timeout_seconds"] == 30 and args["max_output_chars"] == 10000
    done = await executor.decide(r["approval_id"], True, "test")
    assert done["status"] == "done"
    assert done["result"]["exit_code"] == 0 and "hello-pmjc" in done["result"]["output"]


async def test_never_runs_without_approval_even_admin_or_auto_approved():
    from pmjc import auth
    set_cfg(auto_approve=["pmjc_shell_run"])
    for c in (auth.admin(), auth.local(), caller(auto_approve={"pmjc_shell_run"})):
        r = await propose(c, command="echo x")
        assert r["status"] == "pending_approval"


async def test_denied_approval_does_not_run(home):
    from pmjc import executor
    marker = home / "marker.txt"
    r = await propose(caller(), command=f'echo hi > "{marker}"')
    res = await executor.decide(r["approval_id"], False, "test")
    assert res["status"] == "denied" and not marker.exists()
    assert audit("denied_by_user")


async def test_tainted_run_is_refused():
    from pmjc import executor
    with pytest.raises(executor.Denied, match="untrusted"):
        await propose(caller(untrusted=True), command="echo x")
    assert audit("denied")


async def test_jobs_and_watches_denied_by_default_allowed_by_config():
    from pmjc import executor
    job = caller("job:daily", "job")
    watch = caller("watch:w1", "job")
    for c in (job, watch):
        with pytest.raises(executor.Denied, match="shell_command_proposers"):
            await propose(c, command="echo x")
    set_cfg(shell_command_proposers=["owner", "jobs"])
    assert (await propose(job, command="echo x"))["status"] == "pending_approval"
    with pytest.raises(executor.Denied):
        await propose(watch, command="echo x")
    set_cfg(shell_command_proposers=["watches"])
    assert (await propose(watch, command="echo x"))["status"] == "pending_approval"
    with pytest.raises(executor.Denied):
        await propose(caller(), command="echo x")  # owner not listed


async def test_empty_proposers_turns_it_off_and_other_job_kinds_denied():
    from pmjc import executor
    set_cfg(shell_command_proposers=[])
    with pytest.raises(executor.Denied):
        await propose(caller(), command="echo x")
    set_cfg(shell_command_proposers=["owner", "jobs", "watches"])
    with pytest.raises(executor.Denied):  # e.g. an approval follow-up run
        await propose(caller("approval:abc", "job"), command="echo x")


async def test_requested_limits_are_clamped_and_shown_in_approval():
    set_cfg(shell_command_max_timeout_seconds=60, shell_command_max_output_chars=2000)
    r = await propose(caller(), command="echo x", timeout_seconds=45, max_output_chars=500)
    a = stored_args(r["approval_id"])
    assert a["timeout_seconds"] == 45 and a["max_output_chars"] == 500
    r = await propose(caller(), command="echo y", timeout_seconds=9999, max_output_chars=10**9)
    a = stored_args(r["approval_id"])
    assert a["timeout_seconds"] == 60 and a["max_output_chars"] == 2000


async def test_model_cannot_choose_shell_or_cwd(home):
    from pmjc import shell
    r = await propose(caller(), command="echo x", shell="cmd", cwd="/etc")
    a = stored_args(r["approval_id"])
    assert a["shell"] == shell.resolve_shell() and a["cwd"] == str(home)


async def test_overlong_or_empty_command_refused():
    from pmjc import executor, shell
    with pytest.raises(executor.Denied):
        await propose(caller(), command="echo " + "a" * shell.MAX_COMMAND_CHARS)
    with pytest.raises(executor.Denied):
        await propose(caller(), command="   ")


async def test_output_cap_enforced():
    from pmjc import shell
    res = await shell.run('python -c "print(\'x\' * 5000)"', max_output_chars=200)
    assert len(res["output"]) == 200 and res["truncated"]  # a runaway writer is killed past the cap


async def test_timeout_enforced():
    from pmjc import shell
    res = await shell.run('python -c "import time; time.sleep(30)"', timeout_seconds=1)
    assert res["timed_out"] and res["seconds"] < 15


async def test_run_clamps_to_hard_maximum_even_if_stored_value_is_larger():
    from pmjc import shell
    set_cfg(shell_command_max_output_chars=150)
    res = await shell.run('python -c "print(\'x\' * 5000)"', max_output_chars=100000)
    assert res["max_output_chars"] == 150 and len(res["output"]) == 150


async def test_audit_records_proposal_decision_and_result():
    from pmjc import executor
    r = await propose(caller(), command="echo audited-cmd")
    assert audit("approval_requested") and "audited-cmd" in audit("shell_proposed")[0]["detail"]
    await executor.decide(r["approval_id"], True, "tester")
    assert audit("approved_ok")[0]["actor"] == "tester"
    assert "exit 0" in audit("shell_result")[0]["detail"]


async def test_follow_up_after_shell_result_is_tainted(monkeypatch):
    import time
    from pmjc import ask, db, memory
    tid = memory.thread_new("edit a file", by="t")
    db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires,thread_id) "
         "VALUES('s1','pmjc_shell_run','{}','h','t','','done',?,?,?)", time.time(), time.time() + 60, tid)
    seen = {}

    async def fake_ask(c, text, **kw):
        seen["untrusted"] = c.untrusted
        return {"answer": "ok"}
    monkeypatch.setattr(ask, "ask", fake_ask)
    await ask.continue_after_approval("s1", {"status": "done", "result": "out"})
    assert seen["untrusted"] is True


def test_ask_system_names_the_last_resort():
    from pmjc import ask
    assert "pmjc_shell_run" in ask.ASK_SYSTEM and "last resort" in ask.ASK_SYSTEM


async def test_ask_offers_shell_tool_to_actions_callers(monkeypatch):
    from pmjc import agent as agent_mod, ask as ask_mod, db, memory, watches
    db.x(watches.DDL)
    memory.ensure_model_md()
    seen = {}

    async def fake_run(prompt, c, tools, need, system=None, **kw):
        seen["tools"] = {t.name for t in tools}
        return agent_mod.Result("ok", "fake", 0.0)
    monkeypatch.setattr(agent_mod, "run", fake_run)
    await ask_mod.ask(caller(), "edit a file")
    assert "pmjc_shell_run" in seen["tools"]
