"""Command connectors, local files, Home Assistant protection, automating repeated requests."""
from __future__ import annotations

import sys
import textwrap

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import config, creds, db, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load(); s.credential_backend = "file"; config.save(s)
    yield tmp_path
    db._local.c = None


def _cmd_connector(home):
    (home / "connectors.d").mkdir(exist_ok=True)
    py = sys.executable.replace("\\", "/")
    (home / "connectors.d" / "t.yaml").write_text(textwrap.dedent(f'''
        kind: command
        id: t
        tools:
          - name: t_echo
            scope: read
            argv: ["{py}", "-c", "import sys; print(sys.argv[1:])", "{{word}}"]
            params:
              word: {{type: string, pattern: "^[a-z ]+$"}}
          - name: t_pick
            argv: ["{py}", "-c", "print('x')", "{{mode}}"]
            params:
              mode: {{type: string, choices: [a, b]}}
    '''))
    from pmjc import registry
    return registry.reload()


async def test_command_connector_runs_argv_without_shell(home):
    reg = _cmd_connector(home)
    t = reg.tool("t_echo")
    out = await t.fn(word="hello world")
    assert out["exit_code"] == 0 and "['hello world']" in out["stdout"]  # one argv item, no shell splitting
    with pytest.raises(ValueError):
        await t.fn(word="x; rm -rf /")                                  # pattern refuses it
    assert reg.tool("t_pick").scope == "act"                            # fail-closed default
    with pytest.raises(ValueError):
        await reg.tool("t_pick").fn(mode="c")


async def test_command_connector_refuses_option_injection(home):
    reg = _cmd_connector(home)
    (home / "connectors.d" / "t.yaml").write_text((home / "connectors.d" / "t.yaml").read_text().replace('"^[a-z ]+$"', '"^.+$"'))
    from pmjc import registry
    reg = registry.reload()
    with pytest.raises(ValueError):
        await reg.tool("t_echo").fn(word="--all")


async def test_files_stay_inside_roots_and_skip_secrets(home, tmp_path):
    from pmjc import config, registry
    root = tmp_path / "share"; root.mkdir()
    (root / "notes.txt").write_text("meetup on friday")
    (root / "api_token.txt").write_text("nope")
    (root / "secrets").mkdir(); (root / "secrets" / "x.txt").write_text("nope")
    s = config.load(); s.connectors["files"] = {"roots": [str(root)]}; config.save(s)
    reg = registry.reload()
    assert (await reg.tool("files_read").fn(path=str(root / "notes.txt")))["text"] == "meetup on friday"
    names = [e["name"] for e in await reg.tool("files_list").fn(path=str(root))]
    assert names == ["notes.txt"]
    for bad in (root / "api_token.txt", root / "secrets" / "x.txt", tmp_path / "pmjc.db", root / ".." / "settings.yaml"):
        with pytest.raises(PermissionError):
            await reg.tool("files_read").fn(path=str(bad))
    hits = await reg.tool("files_search").fn(query="friday")
    assert [h["path"].endswith("notes.txt") for h in hits] == [True]


async def test_home_assistant_protected_entities(home):
    from pmjc import config, creds, registry
    s = config.load()
    s.connectors["home_assistant"] = {"url": "http://127.0.0.1:9", "blocked_patterns": [r"^switch\.server_power"]}
    config.save(s)
    creds.store().set("home_assistant.token", "t" * 20)
    reg = registry.reload()
    fn = reg.tool("ha_call_service").fn
    with pytest.raises(PermissionError):
        await fn(domain="switch", service="turn_off", entity_id="switch.server_power")
    with pytest.raises(PermissionError):
        await fn(domain="switch", service="turn_off", data={"entity_id": ["switch.lamp", "switch.server_power_2"]})
    with pytest.raises(PermissionError):
        await fn(domain="switch", service="turn_off")  # no target: could hit everything


def test_job_from_request():
    from pmjc import jobs
    j = jobs.job_from_request("What's the weather in Boston?", daily_at="7:05")
    assert j.trigger == {"cron": "5 7 * * *"} and j.notify_when == "not_empty" and "NOTHING" in j.prompt
    assert j.name == "auto-whats-the-weather-in-boston"
    j = jobs.job_from_request("check discord", every_minutes=1)
    assert j.trigger == {"interval": 300}  # at least 5 minutes
    with pytest.raises(ValueError):
        jobs.job_from_request("x", daily_at="25:00")


def test_repeated_requests_are_grouped():
    from pmjc import ask
    for t in ("Weather in Boston?", "weather in boston", "Did Ann reply"):
        ask.record(t, "a", "p", "c")
    rep = ask.repeated()
    assert len(rep) == 1 and rep[0]["times"] == 2 and rep[0]["norm"] == "weather in boston"


def test_ssh_machines_are_generic(home):
    from pmjc import config
    from pmjc.connectors.local import Ssh
    s = config.load()
    s.connectors["ssh"] = {"machines": {
        "nas": {"host": "192.0.2.10", "user": "admin", "port": 2222, "key": "~/.ssh/id_ed25519"},
        "gpu": {"host": "gpu.example.com", "jump": "me@bastion.example.com"},
        "office": {"host": "office-pc", "proxy_command": "cloudflared access ssh --hostname %h"},
        "builder": {"alias": "builder"},
        "evil": {"host": "-oProxyCommand=calc"}}}
    config.save(s)
    c = Ssh()
    a = c._argv("nas", "uptime")
    assert a[-2:] == ["admin@192.0.2.10", "uptime"] and "-p" in a and "2222" in a and "-i" in a
    assert "-J" in c._argv("gpu", "x") and "ProxyCommand=cloudflared access ssh --hostname %h" in c._argv("office", "x")
    assert c._argv("builder", "x")[-2:] == ["builder", "x"]
    with pytest.raises(ValueError):
        c._argv("evil", "x")                  # option injection through the host is refused
    with pytest.raises(PermissionError):
        c._argv("not-configured", "x")
    assert "BatchMode=yes" in c._argv("nas", "x")


def test_ssh_self_route(home, monkeypatch):
    """The machine pmjc runs on is always reachable (no SSH): by hostname, `local`, or a configured name that is the host."""
    import asyncio
    import socket
    import sys
    from pmjc import config
    from pmjc.connectors.local import Ssh
    monkeypatch.setattr(socket, "gethostname", lambda: "HostBox")
    s = config.load()
    s.connectors["ssh"] = {"machines": {"nas": {"host": "192.0.2.10"}, "hostbox": {"alias": "hostbox"},
                                        "tower": {"alias": "tower"}}, "self_names": ["tower"]}
    config.save(s)
    c = Ssh()
    m = c._machines()
    assert m["hostbox"] == {"self": True} and m["tower"] == {"self": True} and "host" in m["nas"]
    shell_exe = c._argv("local", "echo hi")
    assert "ssh" not in shell_exe[0].lower() and shell_exe[-1] == "echo hi"
    assert c._argv("HOSTBOX", "x")[-1] == "x"             # names match case-insensitively
    assert c._argv("nas", "x")[0] == "ssh"                 # other machines still go over SSH
    listed = {x["name"]: x for x in asyncio.run(c.ssh_machines())}
    assert listed["hostbox"]["self"] and listed["hostbox"]["shell"] == ("cmd" if sys.platform == "win32" else "sh")
    r = asyncio.run(c._ssh("local", "echo pmjc-self", 30))
    assert r["exit_code"] == 0 and "pmjc-self" in r["stdout"]
    assert asyncio.run(c.ssh_check("tower"))["self"] is True
    s.connectors["ssh"]["include_self"] = False
    config.save(s)
    with pytest.raises(PermissionError):
        Ssh()._argv("local", "x")


def test_ssh_run_on_self_still_needs_approval(home, monkeypatch):
    import asyncio
    from pmjc import executor, registry
    from pmjc.auth import Caller
    registry.reload()
    c = Caller("remote:laptop", "remote", {"sources", "actions"})
    r = asyncio.run(executor.call_tool(c, "ssh_run", {"machine": "local", "command": "echo hi"}))
    assert r["status"] == "pending_approval"


def test_pmjc_tokens_satisfy_oauth_transport_scopes(home):
    """With Google OAuth on, /mcp requires openid+email on every token; pmjc's own tokens must pass that check
    (they used to get 403) while keeping their pmjc scopes."""
    import asyncio
    from pmjc import auth, mcp_server
    tok = auth.create_token("phone", ["sources", "actions"])
    v = mcp_server.PmjcVerifier()
    assert set(asyncio.run(v.verify_token(tok)).scopes) == {"sources", "actions"}
    v.transport_scopes = ("openid", "email")
    got = asyncio.run(v.verify_token(tok))
    assert {"openid", "email", "sources", "actions"} <= set(got.scopes) and "admin" not in got.scopes
    assert asyncio.run(v.verify_token("pmjc_wrong")) is None
