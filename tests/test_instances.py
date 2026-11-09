"""Multi-instance connectors: `<type>:<instance>` keys in connectors:, namespaced credentials and tool names."""
from __future__ import annotations

import textwrap

import pytest
from fastapi.testclient import TestClient

LONG = "mail_a_very_long_tool_name_that_goes_on_and_on_for_a_while_ok"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "test-master-key")
    from pmjc import config, creds, db, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load()
    s.credential_backend = "file"
    config.save(s)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "mail.py").write_text(textwrap.dedent('''
        from pmjc.connector import Connector, Field, Health, tool

        class Mail(Connector):
            id = "mail"
            name = "Mail"
            credentials = [Field("host", "Host", secret=False), Field("password", "Password")]

            async def health(self):
                return Health(self.configured(), "host=" + str(self.option("host")))

            @tool(scope="read")
            async def mail_search(self, q: str) -> str:
                "Search mail."
                return f"{self.option('host')}|{self.secret('password')}|{q}"

            @tool(scope="read", name="%s")
            async def long_one(self) -> str:
                "Long name."
                return "x"
    ''' % LONG), encoding="utf-8")
    return tmp_path


def _setup(instances: dict[str, dict]):
    from pmjc import config, registry
    s = config.load()
    s.connectors.update(instances)
    config.save(s)
    registry._reg = None
    return registry.get()


def test_two_instances_options_and_secrets():
    from pmjc import creds
    reg = _setup({"mail": {"host": "h1"}, "mail:work": {"host": "h2"}})
    creds.store().set("mail.password", "pw-default")
    creds.store().set("mail:work.password", "pw-work")
    assert reg.connectors["mail"].option("host") == "h1"
    assert reg.connectors["mail:work"].option("host") == "h2"
    assert reg.connectors["mail"].secret("password") == "pw-default"
    assert reg.connectors["mail:work"].secret("password") == "pw-work"
    assert reg.connectors["mail:work"].type_id == "mail" and reg.connectors["mail:work"].instance == "work"
    assert reg.connectors["mail"].instance == "" and reg.connectors["mail"].type_id == "mail"


@pytest.mark.asyncio
async def test_tool_names_and_calls():
    from pmjc import auth, creds, executor
    reg = _setup({"mail": {"host": "h1"}, "mail:work": {"host": "h2"}, "mail:my-home": {"host": "h3"}})
    for k in ("mail", "mail:work", "mail:my-home"):
        creds.store().set(f"{k}.password", "pw-" + k)
    names = {t.name: t for t in reg.tools()}
    assert {"mail_search", "mail_search__work", "mail_search__my_home"} <= set(names)
    assert names["mail_search"].connector == "mail"
    assert names["mail_search__work"].connector == "mail:work"
    assert names["mail_search__work"].doc.startswith("[work] Search mail.")
    assert names["mail_search__work"].raw_doc == "[work] Search mail."
    assert names["mail_search"].raw_doc == "Search mail."
    assert await executor.call_tool(auth.admin(), "mail_search__work", {"q": "a"}) == "h2|pw-mail:work|a"
    assert await executor.call_tool(auth.admin(), "mail_search", {"q": "a"}) == "h1|pw-mail|a"
    assert all(len(n) <= 64 for n in names)
    long = sorted(n for n in names if n.startswith("mail_a_very_long"))
    assert len(long) == 3 and len(set(long)) == 3          # truncated names stay distinct and valid
    assert any(n.endswith("__work") for n in long) and any(n.endswith("__my_home") for n in long)


def test_default_instance_unchanged():
    reg = _setup({"mail": {"host": "h1"}})
    assert "mail:work" not in reg.connectors
    tools = [t for t in reg.tools(include_unconfigured=True) if t.connector == "mail"]
    assert {t.name for t in tools} == {"mail_search", LONG}
    assert all(not t.doc.startswith("[") for t in tools)
    from pmjc.registry import instance_tool_name
    assert instance_tool_name("x_y", "") == "x_y"


def test_bad_instances_reported_and_disabled_type():
    from pmjc import config, registry
    reg = _setup({"nosuch:x": {}, "mail:bad name": {}})
    assert "nosuch:x" in reg.errors and "mail:bad name" in reg.errors
    assert "nosuch:x" not in reg.connectors and "mail:bad name" not in reg.connectors
    s = config.load()
    s.disabled_connectors = ["mail"]
    s.connectors["mail:work"] = {}
    config.save(s)
    registry._reg = None
    assert "mail:work" not in registry.get().connectors


@pytest.mark.asyncio
async def test_health_per_instance():
    from pmjc import creds
    reg = _setup({"mail": {"host": "h1"}, "mail:work": {"host": "h2"}, "mail:other": {}})
    creds.store().set("mail.password", "a")
    creds.store().set("mail:work.password", "b")
    h = {i: await reg.connectors[i].health() for i in ("mail", "mail:work", "mail:other")}
    assert h["mail"].ok and h["mail:work"].ok and not h["mail:other"].ok
    assert h["mail:work"].detail == "host=h2"
    assert not any(t.connector == "mail:other" for t in reg.tools())  # unconfigured instance offers no tools


def test_rest_credentials_create_instance_and_describe():
    from pmjc import auth, config, creds, server
    _setup({"mail": {"host": "h1"}})
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    adm = {"Authorization": "Bearer " + auth.create_token("dash", ["admin"])}
    r = c.put("/api/connectors/mail:work/credentials", json={"values": {"host": "h9", "password": "pw9"}}, headers=adm)
    assert r.status_code == 200, r.text
    assert config.load().connectors["mail:work"]["host"] == "h9"
    assert creds.store().get("mail:work.password") == "pw9"
    assert creds.store().get("mail.password") is None
    items = {x["id"]: x for x in c.get("/api/connectors", headers=adm).json()["connectors"]}
    assert items["mail:work"]["instance"] == "work" and items["mail:work"]["type"] == "mail"
    assert items["mail:work"]["health"]["ok"] and "mail" in items and items["mail"]["instance"] == ""
    assert c.put("/api/connectors/nosuch:x/credentials", json={"values": {}}, headers=adm).status_code == 404
    assert c.put("/api/connectors/mail:bad name/credentials", json={"values": {}}, headers=adm).status_code == 404
    creds.store().set("mail.password", "keep")  # clearing an instance leaves the default alone
    assert c.delete("/api/connectors/mail:work/credentials", headers=adm).status_code == 200
    assert creds.store().get("mail:work.password") is None and creds.store().get("mail.password") == "keep"
    assert "mail:work" not in config.load().connectors and "mail" in config.load().connectors


def test_cli_auth_set_creates_instance(monkeypatch):
    from typer.testing import CliRunner
    from pmjc import config, creds
    from pmjc.cli import app
    _setup({"mail": {"host": "h1"}})
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "cli-secret")
    monkeypatch.setattr("typer.prompt", lambda *a, **k: "cli-host")
    r = CliRunner().invoke(app, ["auth", "set", "mail:work"])
    assert r.exit_code == 0, r.output
    assert creds.store().get("mail:work.password") == "cli-secret"
    assert config.load().connectors["mail:work"]["host"] == "cli-host"
    assert creds.store().get("mail.password") is None
    assert CliRunner().invoke(app, ["auth", "set", "nosuch:x"]).exit_code != 0


def test_env_credential_for_instance(monkeypatch):
    from pmjc import creds
    reg = _setup({"mail:work": {"host": "h2"}})
    monkeypatch.setenv("PMJC_MAIL_WORK_PASSWORD", "from-env")
    assert reg.connectors["mail:work"].secret("password") == "from-env"
    assert creds.env_name("mail:work.password") == "PMJC_MAIL_WORK_PASSWORD"


@pytest.mark.asyncio
async def test_pmjc_connectors_lists_instances():
    from pmjc import auth, executor
    _setup({"mail": {"host": "h1"}, "mail:work": {"host": "h2"}})
    out = await executor.call_tool(auth.admin(), "pmjc_connectors", {})
    ids = {c["id"]: c for c in out}
    assert ids["mail:work"]["instance"] == "work" and ids["mail:work"]["type"] == "mail"
    assert ids["mail"]["instance"] is None
