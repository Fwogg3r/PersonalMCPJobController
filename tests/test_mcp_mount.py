"""Existing MCP servers mounted as connectors (phase4/b): discovery, safe defaults, executor path, health."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

FAKE = str(Path(__file__).with_name("mcp_fake_server.py"))


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
    config.save(s)
    yield tmp_path
    db._local.c = None
    registry._reg = None


@pytest.fixture(autouse=True, scope="module")
def _close_servers():
    yield
    from pmjc import mcp_connector
    mcp_connector.shutdown()


def mount(**kw):
    from pmjc import config, registry
    entry = {"id": "fake", "transport": "stdio", "command": sys.executable, "args": [FAKE], "timeout": 30} | kw
    s = config.load()
    s.mcp_servers = [config.McpServerConfig(**entry)]
    config.save(s)
    return registry.reload()


def caller(**kw):
    from pmjc import auth
    c = auth.Caller("agent", "remote", {"sources", "actions", "jobs"})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_discovery_naming_and_schema_passthrough():
    from pmjc import registry
    reg = mount()
    names = {t.name for t in reg.tools()}
    assert {"fake__lookup", "fake__send_note", "fake__danger"} <= names
    weird = [n for n in names if n.startswith("fake__weird")]
    assert weird and all(len(n) <= 64 for n in weird)
    t = reg.tool("fake__lookup")
    assert t.doc.startswith("Look something up")
    schema = registry.tool_json_schema(t)
    assert schema["properties"]["query"]["type"] == "string" and schema["required"] == ["query"]
    compact = registry.compact_tool_schema(t)
    assert "title" not in compact["properties"]["query"] and compact["properties"]["limit"]["default"] == 3
    from pmjc.connector import TOOL_NAME
    assert all(TOOL_NAME.match(n) for n in names)


def test_long_names_are_capped():
    from pmjc.mcp_connector import sanitize_name
    n = sanitize_name("a" * 30, "b" * 80)
    assert len(n) <= 64 and n.startswith("a" * 30 + "__")
    assert sanitize_name("x", "a.b c") == "x__a_b_c"


def test_scope_defaults_follow_read_only_hint():
    reg = mount()
    assert reg.tool("fake__lookup").scope == "read"
    assert reg.tool("fake__send_note").scope == "act"
    for n in ("fake__lookup", "fake__send_note"):
        t = reg.tool(n)
        assert t.untrusted_output and t.egress


async def test_act_tool_needs_approval_through_executor():
    from pmjc import executor
    mount()
    res = await executor.call_tool(caller(), "fake__send_note", {"text": "hi"})
    assert res["status"] == "pending_approval"
    done = await executor.decide(res["approval_id"], True, "tester")
    assert done["status"] == "done" and "sent hi" in str(done["result"])


async def test_read_tool_runs_and_taints_the_run():
    from pmjc import executor
    mount()
    c = caller()
    assert not c.untrusted
    res = await executor.call_tool(c, "fake__lookup", {"query": "cats", "limit": 2})
    assert "found cats x2" in str(res)
    assert c.untrusted  # untrusted_output taints
    with pytest.raises(executor.Denied):  # and egress is refused afterwards
        await executor.call_tool(c, "fake__lookup", {"query": "dogs"})


def test_allowlist_and_exclude():
    reg = mount(tools=["lookup", "danger"], exclude=["danger"])
    assert {t.name for t in reg.tools() if t.connector == "fake"} == {"fake__lookup"}


def test_per_tool_overrides():
    reg = mount(overrides={"lookup": {"scope": "act", "egress": False, "untrusted_output": False},
                           "send_note": {"scope": "read"}})
    lk, sn = reg.tool("fake__lookup"), reg.tool("fake__send_note")
    assert (lk.scope, lk.egress, lk.untrusted_output) == ("act", False, False)
    assert sn.scope == "read" and sn.egress and sn.untrusted_output


async def test_failed_server_is_unhealthy_without_breaking_others():
    from pmjc import config, registry
    s = config.load()
    s.mcp_servers = [config.McpServerConfig(id="broken", command="definitely-not-a-real-binary-xyz", timeout=5),
                     config.McpServerConfig(id="fake", command=sys.executable, args=[FAKE], timeout=30)]
    config.save(s)
    reg = registry.reload()
    h_bad, h_ok = await reg.connectors["broken"].health(), await reg.connectors["fake"].health()
    assert not h_bad.ok and "failed" in h_bad.detail
    assert h_ok.ok and "4 tools" in h_ok.detail
    assert reg.tool("fake__lookup") and not [t for t in reg.tools() if t.connector == "broken"]
    assert "weather" in reg.connectors  # built-ins unaffected


def test_disabled_server_is_not_mounted():
    reg = mount(enabled=False)
    assert "fake" not in reg.connectors


def test_invalid_entries_are_rejected():
    from pydantic import ValidationError
    from pmjc import config
    with pytest.raises(ValidationError):
        config.McpServerConfig(id="x", transport="stdio")
    with pytest.raises(ValidationError):
        config.McpServerConfig(id="x", transport="http", url="ftp://h")
    with pytest.raises(ValidationError):
        config.McpServerConfig(id="bad id", command="x")


def test_argv_cannot_be_set_through_the_api():
    from fastapi.testclient import TestClient
    from pmjc import auth, config, server
    mount()
    before = config.load().mcp_servers[0].model_dump()
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    adm = {"Authorization": "Bearer " + auth.create_token("dash", ["admin"])}
    evil = {"mcp_servers": [{"id": "evil", "command": "calc.exe"}], "command": "calc.exe", "args": ["x"]}
    c.put("/api/settings/general", json={"monitoring_enabled": False, **evil}, headers=adm)
    r = c.put("/api/connectors/fake/credentials", json={"values": {"command": "calc.exe", "args": "x"}}, headers=adm)
    assert r.status_code == 400  # an mcp connector declares no settable fields
    assert c.put("/api/routing", json={"mcp_servers": evil["mcp_servers"]}, headers=adm).status_code in (400, 422)
    after = config.load().mcp_servers
    assert [e.id for e in after] == ["fake"] and after[0].model_dump() == before
    tools = c.get("/api/tools", headers=adm).json()
    assert any(t["name"] == "fake__lookup" and t["parameters"]["required"] == ["query"] for t in tools)
