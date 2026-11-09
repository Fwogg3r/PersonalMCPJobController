"""Compact tool schemas for small-context providers (schema_mode: compact)."""
from __future__ import annotations

import json

import pytest

from pmjc import agent, registry
from pmjc.config import ProviderConfig
from pmjc.registry import ToolSpec


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import config, creds, db
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load(); s.credential_backend = "file"; config.save(s)
    yield tmp_path
    db._local.c = None


def _fn(query: str, limit: int | None = None, mode: str = "fast", tags: list[str] | None = None):
    """Search things.

    Longer explanation that a small model does not need.

    Returns rows."""


def _spec(name="x_search", connector="x"):
    return ToolSpec(name, connector, "read", _fn, _fn.__doc__ + "\nLimits: only things.\nBetter if your client can: x.",
                    False, raw_doc=_fn.__doc__)


def _prov(mode):
    return ProviderConfig(id="p", kind="openai_compat", schema_mode=mode)


def test_default_is_full():
    assert ProviderConfig(id="p", kind="openai_compat").schema_mode == "full"


def test_full_payload_equals_old_output():
    tools = [_spec("b_tool", "b"), _spec("a_tool", "a")]
    schema, system = agent.tool_payload(_prov("full"), tools, "SYS", "openai")
    assert system == "SYS"
    assert schema == [{"type": "function", "function": {"name": t.name, "description": t.doc[:1000],
                                                        "parameters": registry.tool_json_schema(t)}} for t in tools]
    a, _ = agent.tool_payload(_prov("full"), tools, "SYS", "anthropic")
    assert a == [{"name": t.name, "description": t.doc[:1000], "input_schema": registry.tool_json_schema(t)}
                 for t in tools]


def test_compact_schema():
    t = _spec()
    full = registry.tool_json_schema(t)
    assert "title" in full["properties"]["query"] and "anyOf" in full["properties"]["limit"]
    c = registry.compact_tool_schema(t)
    assert "title" not in json.dumps(c)
    assert c["required"] == ["query"] == full["required"]
    assert c["properties"]["limit"] == {"type": "integer"}  # flattened, null default dropped
    assert c["properties"]["mode"]["default"] == "fast"
    assert c["properties"]["tags"]["type"] == "array"
    assert registry.compact_tool_doc(t) == "Search things."


def test_long_default_dropped():
    c = registry.compact_json_schema({"type": "object", "properties": {"a": {"title": "A", "default": "x" * 100}}})
    assert c == {"type": "object", "properties": {"a": {}}}


def test_property_named_title_survives():
    c = registry.compact_json_schema(
        {"type": "object", "title": "T", "properties": {"title": {"type": "string", "title": "Title"}}})
    assert c == {"type": "object", "properties": {"title": {"type": "string"}}}


def test_compact_payload_sorted_and_valid_openai_body():
    tools = [_spec("b_tool", "b"), _spec("a_tool", "a")]
    schema, _ = agent.tool_payload(_prov("compact"), tools, "SYS", "openai")
    assert [s["function"]["name"] for s in schema] == ["a_tool", "b_tool"]
    body = json.loads(json.dumps({"model": "m", "messages": [], "tools": schema}))
    for s in body["tools"]:
        params = s["function"]["parameters"]
        assert params["type"] == "object" and "query" in params["properties"]
        assert s["function"]["description"] == "Search things."


def test_limits_once_per_connector_only_for_present_connectors():
    reg = registry.get()
    ids = [i for i, c in reg.connectors.items() if c.limitations]
    assert len(ids) >= 2
    tools = [_spec("t1", ids[0]), _spec("t2", ids[0])]
    _, system = agent.tool_payload(_prov("compact"), tools, "SYS", "openai")
    assert system.count(f"- {ids[0]}:") == 1
    assert f"- {ids[1]}:" not in system
    assert "Better if" not in system
    assert " ".join(reg.connectors[ids[0]].limitations.split()) in system
    _, none = agent.tool_payload(_prov("compact"), [], "SYS", "openai")
    assert none == "SYS"


def test_real_toolset_compact_is_smaller():
    tools = registry.get().tools()
    f, _ = agent.tool_payload(_prov("full"), tools, "", "openai")
    c, s = agent.tool_payload(_prov("compact"), tools, "", "openai")
    assert len(json.dumps(c)) + len(s) < len(json.dumps(f))
    def has_title_keyword(n):
        if isinstance(n, dict):
            return any(k == "title" and not isinstance(v, dict) or (k != "properties" and has_title_keyword(v))
                       or (k == "properties" and any(has_title_keyword(pv) for pv in v.values()))
                       for k, v in n.items())
        return isinstance(n, list) and any(has_title_keyword(x) for x in n)
    assert not any(has_title_keyword(x["function"]["parameters"]) for x in c)
    assert {x["function"]["name"] for x in c} == {x["function"]["name"] for x in f}
