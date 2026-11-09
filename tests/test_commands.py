"""Snappy commands: don't wait on inherited pipes, `running` + run_id for long commands, command_status, visibility.

Everything runs on the self route with `sys.executable -c` (the shell argv is patched), never real SSH, never a window.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "test-master-key")
    from pmjc import commands, config, creds, db, memory, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    commands.reset()
    s = config.load()
    s.credential_backend = "file"
    s.shell_command_cwd = str(tmp_path)
    s.approval_poll_seconds = 0.05
    config.save(s)
    from pmjc.connectors.local import Ssh
    monkeypatch.setattr(Ssh, "_argv", lambda self, name, command: [sys.executable, "-c", command])
    yield tmp_path
    db._local.c = None


def caller(name="agent", kind="remote"):
    from pmjc import auth
    return auth.Caller(name, kind, {"sources", "actions", "jobs"})


def ssh():
    from pmjc import registry
    return registry.get().connectors["ssh"]


def set_wait(n):
    from pmjc import config
    s = config.load()
    s.commands.wait_seconds = n
    config.save(s)


async def call(c, tool, **args):
    from pmjc import executor
    return await executor.call_tool(c, tool, args)


def kill(pid):
    import psutil
    try:
        psutil.Process(int(pid)).kill()
    except Exception:
        pass


def test_default_wait_seconds_is_8():
    from pmjc import config
    assert config.load().commands.wait_seconds == 8


async def test_short_command_returns_the_full_result():
    r = await ssh().ssh_run("local", "import sys; print('hi'); print('err', file=sys.stderr); sys.exit(3)")
    assert r["exit_code"] == 3 and r["stdout"].strip() == "hi" and r["stderr"].strip() == "err"
    assert "detached_children" not in r and "status" not in r


async def test_child_holding_the_pipes_does_not_block():
    code = ("import subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "print(p.pid, flush=True)\n")
    t0 = time.time()
    r = await ssh().ssh_run("local", code, wait_seconds=8)
    took = time.time() - t0
    try:
        assert took < 3, took
        assert r["exit_code"] == 0 and r["detached_children"] is True
        assert r["stdout"].strip().isdigit()  # what was printed is still returned
    finally:
        kill(r["stdout"].strip())


async def test_long_command_returns_running_then_status_reports_done():
    from pmjc import commands
    code = "import time\nprint('a', flush=True)\ntime.sleep(2.5)\nprint('b', flush=True)"
    t0 = time.time()
    r = await ssh().ssh_run("local", code, wait_seconds=1)
    assert time.time() - t0 < 2.2
    assert r["status"] == "running" and r["run_id"] and "command_status" in r["next"]
    assert r["output_so_far"].strip() == "a"
    s1 = await commands.status(r["run_id"], 0)
    assert s1["status"] == "running" and s1["output"].strip() == "a"
    s2 = await commands.status(r["run_id"], 10)  # waits for the end
    assert s2["status"] == "done" and s2["exit_code"] == 0 and s2["output"].strip() == "b"
    from pmjc import db
    row = db.q("SELECT status, exit_code FROM command_runs WHERE run_id=?", r["run_id"])[0]
    assert row["status"] == "done" and row["exit_code"] == 0


async def test_default_wait_comes_from_config():
    set_wait(1)
    r = await ssh().ssh_run("local", "import time; time.sleep(2)")
    assert r["status"] == "running"
    await call(caller(), "command_status", run_id="nope", wait_seconds=0)
    from pmjc import commands
    assert (await commands.status(r["run_id"], 10))["status"] == "done"


async def test_timeout_still_applies_to_background_commands():
    from pmjc import commands
    r = await ssh().ssh_run("local", "import time; time.sleep(60)", timeout_seconds=5, wait_seconds=1)
    assert r["status"] == "running"
    s = await commands.status(r["run_id"], 12)
    assert s["status"] == "done" and s["timed_out"] is True


async def test_command_status_visibility_and_taint(monkeypatch):
    from pmjc import auth, commands, executor, registry
    t = registry.get().tool("command_status")
    assert t.scope == "read" and t.untrusted_output
    assert registry.get().tool("ssh_run").untrusted_output and registry.get().tool("pmjc_shell_run").untrusted_output
    set_wait(1)
    # an approved ssh_run owned by "agent"
    owner = caller("agent")
    p = await call(owner, "ssh_run", machine="local", command="import time; time.sleep(2)")
    res = await executor.decide(p["approval_id"], True, "owner")
    rid = res["result"]["run_id"]
    mine = await call(owner, "command_status", run_id=rid, wait_seconds=0)
    assert mine["status"] in ("running", "done") and owner.untrusted  # output is untrusted: taints the run
    stranger = await call(caller("someone-else"), "command_status", run_id=rid, wait_seconds=0)
    assert stranger == {"status": "unknown", "run_id": rid}
    admin = await call(auth.admin(), "command_status", run_id=rid, wait_seconds=0)
    assert admin["run_id"] == rid and admin["status"] in ("running", "done")
    # a stranger can't stop it either
    other = await commands.stop(rid, caller("someone-else"))
    assert other["status"] == "unknown"
    await commands.status(rid, 10)


async def test_approved_slow_run_does_not_hold_the_approval_wait():
    from pmjc import commands, executor
    set_wait(1)
    c = caller("agent")
    p = await call(c, "ssh_run", machine="local", command="import time; time.sleep(4)")
    assert p["status"] == "pending_approval"
    waiter = asyncio.create_task(executor.wait_for_decision(p["approval_id"], 30, deliver=True))
    await asyncio.sleep(0.1)
    t0 = time.time()
    await executor.decide(p["approval_id"], True, "owner")
    out = await waiter
    assert time.time() - t0 < 3
    assert out["status"] == "done" and out["result"]["status"] == "running" and out["result"]["run_id"]
    # the act ledger recorded it once
    from pmjc import db
    assert len(db.q("SELECT * FROM approvals WHERE id=? AND status='done'", p["approval_id"])) == 1
    s = await commands.status(out["result"]["run_id"], 10, c)
    assert s["status"] == "done"


async def test_command_stop_kills_a_running_command():
    from pmjc import commands, executor
    set_wait(1)
    c = caller("agent")
    p = await call(c, "ssh_run", machine="local", command="import time; time.sleep(60)")
    rid = (await executor.decide(p["approval_id"], True, "owner"))["result"]["run_id"]
    sp = await call(c, "command_stop", run_id=rid)
    assert sp["status"] == "pending_approval"
    out = (await executor.decide(sp["approval_id"], True, "owner"))["result"]
    assert out["status"] == "done" and out["stopped"] is True
    assert (await commands.status(rid, 0, c))["status"] == "done"


async def test_shell_run_returns_running_and_detached(home):
    from pmjc import commands, shell
    set_wait(1)
    amp = "& " if sys.platform == "win32" else ""
    r = await shell.run(f'{amp}"{sys.executable}" -c "import time; time.sleep(2.5); print(7)"', wait_seconds=1)
    assert r["status"] == "running"
    s = await commands.status(r["run_id"], 10)
    assert s["status"] == "done" and s["exit_code"] == 0 and "7" in s["output"]
    quick = await shell.run(f'{amp}"{sys.executable}" -c "print(5)"')
    assert quick["exit_code"] == 0 and quick["output"].strip() == "5"


def test_cmd_command_line_keeps_quotes():
    """cmd.exe gets the command verbatim (Python's list quoting would turn inner quotes into \\" and break it)."""
    import os
    from pmjc.commands import popen_args
    argv = [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", r'echo "a b" & dir "C:\x y"']
    out = popen_args(argv)
    if os.name == "nt":
        assert out == r'"C:\Windows\System32\cmd.exe" /d /s /c "echo "a b" & dir "C:\x y""'
    else:
        assert out == argv
    assert popen_args(["sh", "-c", "echo hi"]) == ["sh", "-c", "echo hi"]
