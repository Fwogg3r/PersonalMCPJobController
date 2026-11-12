"""The smoke harness (pmjc.evals.smoke): acts are only recorded, nothing reaches a person, the bypass stays local."""
from __future__ import annotations

import pathlib

from test_context import home  # noqa: F401  (autouse temp PMJC_HOME)

from pmjc import config
from pmjc.connector import Connector, tool
from pmjc.evals import smoke, tasks


class Thing(Connector):
    id = "thing"

    @tool(scope="read")
    async def thing_read(self) -> str:
        """Read."""
        return "real read"

    @tool(scope="act")
    async def thing_send(self, to: str, text: str = "") -> str:
        """Send."""
        raise AssertionError("a real act ran in the smoke test")


async def test_dry_connector_records_acts_and_keeps_reads():
    smoke.RECORDED.clear()
    d = smoke.DryConnector(Thing())
    fns = {meta["name"]: fn for meta, fn in d.tools()}
    assert await fns["thing_read"]() == "real read"
    out = await fns["thing_send"](to="x", text="hi", _caller=None)
    assert out["dry_run"] is True
    assert smoke.RECORDED == [{"tool": "thing_send", "args": {"to": "x", "text": "hi"}}]
    import inspect
    assert list(inspect.signature(fns["thing_send"]).parameters) == ["to", "text"]  # schema the model sees is kept
    assert d.id == "thing"


def test_settings_reach_nobody():
    s = config.load().model_copy(deep=True)
    s.notify_channels = ["discord"]
    s.discord_door.enabled = True
    s.scheduled_jobs_enabled = True
    s.monitoring_enabled = True
    p = config.ProviderConfig(id="loc", kind="openai_compat", trust="local")
    out = smoke.smoke_settings(s, p)
    assert out.notify_channels == [] and not out.discord_door.enabled and not out.scheduled_jobs_enabled
    assert not out.monitoring_enabled and not out.approval_outcome_notify and out.grants == []
    assert [r.id for r in out.routing] == ["loc"]


def test_only_the_smoke_module_builds_dry_connectors():
    src = pathlib.Path(smoke.__file__).parents[1]
    users = [p for p in src.rglob("*.py") if "DryConnector(" in p.read_text(encoding="utf-8")]
    assert [p.name for p in users] == ["smoke.py"]


def test_smoke_prompts_load_and_are_generic():
    ts = smoke.load_tasks()
    assert len(ts) >= 50
    text = "".join(p.read_text(encoding="utf-8") for p in smoke.SMOKE_DIR.glob("*.yaml")).lower()
    import re
    assert not re.search(r"[a-z]:[\\/]users|\\\\\\\\|/home/|@(?!example\.)[a-z0-9-]+\.[a-z]{2,}", text)  # no user paths, shares, emails
    assert all(isinstance(t, tasks.Task) for t in ts)


def test_live_home_copies_owner_connectors(monkeypatch):
    real = config.home()
    (real / "connectors.d").mkdir(exist_ok=True)
    (real / "connectors.d" / "mine.yaml").write_text("kind: command\nid: mine\n")
    monkeypatch.setattr(smoke, "_real_secrets", lambda: {})
    p = config.ProviderConfig(id="x", kind="openai", base_url="http://127.0.0.1:1/v1", model="m")
    monkeypatch.setattr(smoke.registry.Registry, "load", lambda self: self)
    with smoke.live_home(p) as d:
        assert (d / "connectors.d" / "mine.yaml").is_file() and d != real
