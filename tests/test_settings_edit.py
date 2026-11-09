"""Configuring pmjc from chat: allowlist, hard denies, taint and caller gates, compare-and-swap, patching, backups,
revert, profiles, approval rendering and the two-step confirm. Temp PMJC_HOME only."""
from __future__ import annotations

import json
import time

import pytest
import yaml

from pmjc import config, db, executor, settings_edit
from pmjc.executor import Denied

SET, REVERT, PROFILE = "pmjc_settings_set", "pmjc_settings_revert", "pmjc_profile_switch"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "test-master-key")
    from pmjc import creds, memory, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    memory.reset()
    s = config.load()
    s.credential_backend = "file"
    config.save(s)
    yield tmp_path
    db._local.c = None


def caller(name="owner-chat", kind="remote", surface="v1", thread=None, **kw):
    from pmjc import auth
    c = auth.Caller(name, kind, {"sources", "actions", "jobs"}, surface=surface, thread_id=thread)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


async def propose(c=None, changes=None, reason="because", tool=SET, **extra):
    args = {"changes": changes, "reason": reason} if tool == SET else extra
    if tool == PROFILE:
        args = {"name": extra["name"]}
    return await executor.call_tool(c or caller(), tool, args)


def write_yaml(text):
    config.path("settings.yaml").write_text(text, encoding="utf-8")


def stored(aid):
    return json.loads(db.q("SELECT args FROM approvals WHERE id=?", aid)[0]["args"])


def audit(status):
    return db.q("SELECT * FROM audit WHERE status=?", status)


async def approve(aid):
    return await executor.decide(aid, True, "tester")


# --- the registry and the hard deny ------------------------------------------------------------------------------
HARD = [
    "auto_approve", "allowed_hosts", "host", "port", "public_url", "credential_backend", "oauth.enabled",
    "oauth.allowed", "enabled_plugins", "mcp_servers", "connectors", "connectors.web", "discord_door.log_channel_id",
    "discord_door.enabled", "notify_channels", "monitoring_sources", "shell_command_proposers", "shell_command_cwd",
    "output_link_allowlist", "audit_retention_days", "approval_ttl_seconds", "approval_poll_seconds", "research.tools",
    "front_door.enabled", "front_door.rate_limit_per_minute", "front_door.max_body_bytes", "front_door.keepalive_seconds",
    "router.gpu_busy_processes", "profiles", "routing",
] + [f"routing[openai-api].{f}" for f in sorted(settings_edit.HARD_DENY_PROVIDER_FIELDS)]


@pytest.mark.parametrize("path", HARD)
async def test_every_hard_denied_path_is_refused(path):
    assert settings_edit.hard_denied(path)
    with pytest.raises(Denied, match="can't be changed from chat"):
        await propose(changes={path: "x"})
    assert not db.q("SELECT 1 FROM approvals")


async def test_hard_deny_beats_a_registry_entry(monkeypatch):
    monkeypatch.setitem(settings_edit.SPECS, "auto_approve", settings_edit.Spec("connlist", "off-lower"))
    with pytest.raises(Denied, match="can't be changed"):
        await propose(changes={"auto_approve": ["pmjc_shell_run"]})


async def test_unknown_and_malformed_paths_are_refused():
    for p in ("router.nonsense", "nope", "router.sticky.x", "routing[nope].enabled", "router.sticky\u200b", "router.tiers",
              "router.route_rules", "../x"):
        with pytest.raises(Denied):
            await propose(changes={p: True})


# --- directions and bounds ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("changes", [
    {"monitoring_enabled": True},  # off only (and it is off now; turning on is refused)
    {"scheduled_jobs_enabled": True},
    {"routing[openai-api].enabled": True},
    {"approval_outcome_notify": False},
    {"thread_retention_days": 30},  # upward only
    {"thread_retention_days": 400},  # cap
    {"result_ttl_hours": 48},  # down only
    {"research.max_turns": 12},
    {"research.max_chars": 5000},
    {"routing[anthropic-api].monthly_budget_usd": 50},  # never upward
    {"routing[anthropic-api].monthly_budget_usd": None},  # never unlimited
    {"routing[anthropic-api].select_budget_tokens": 500},
    {"routing[claude-plan].max_result_chars": 99999},
    {"router.gpu_busy_action": "cloud"},
    {"router.gpu_busy_jobs": "cloud"},
    {"router.gpu_busy_backoff_seconds": 0},
    {"router.gpu_busy_max_waits": 500},
    {"router.stay_tool_calls": 0},
    {"router.stay_history_share": 1.5},
    {"routing[lmstudio-local].condense_at": 0.1},
    {"front_door.history_turns": 50},
    {"commands.wait_seconds": 1},
    {"approval_wait_seconds": 120},
    {"router.footer": "yes"},  # type
    {"router.sticky": 1},  # bool is not an int
    {"router.footer_surfaces": ["discord", "carrier-pigeon"]},
    {"disabled_connectors": ["pmjc"]},  # the approval path itself
])
async def test_direction_bound_and_type_violations_are_refused(changes):
    if "thread_retention_days" in changes:
        s = config.load()
        s.thread_retention_days = 90
        config.save(s)
    with pytest.raises(Denied):
        await propose(changes=changes)


async def test_none_budget_refused_in_apply_even_if_validation_were_bypassed(monkeypatch):
    s = config.load()
    new = s.model_copy(deep=True)
    new.routing[2].monthly_budget_usd = None
    with pytest.raises(settings_edit.SettingsError, match="unlimited"):
        settings_edit.check_invariants(s, new)


async def test_disabled_connectors_add_only_and_not_notify_or_door():
    s = config.load()
    s.notify_channels = ["slack"]
    config.save(s)
    with pytest.raises(Denied, match="can't be disabled"):
        await propose(changes={"disabled_connectors": ["slack"]})
    s = config.load()
    s.disabled_connectors = ["weather"]
    config.save(s)
    with pytest.raises(Denied, match="only add"):
        await propose(changes={"disabled_connectors": []})
    r = await propose(changes={"disabled_connectors": ["weather", "places"]})
    assert r["status"] == "pending_approval"


async def test_max_three_paths_and_one_risk_class():
    four = {"router.footer": False, "front_door.stream_progress": False, "approval_wait_seconds": 20,
            "commands.wait_seconds": 5}
    with pytest.raises(Denied, match="at most 3"):
        await propose(changes=four)
    with pytest.raises(Denied, match="one risk class"):
        await propose(changes={"router.footer": False, "router.gpu_busy_action": "local"})
    three = dict(list(four.items())[:3])
    assert (await propose(changes=three))["status"] == "pending_approval"


async def test_reason_required_and_noop_refused():
    with pytest.raises(Denied, match="reason"):
        await propose(changes={"router.footer": False}, reason="")
    with pytest.raises(Denied, match="already"):
        await propose(changes={"router.footer": True})


# --- callers -----------------------------------------------------------------------------------------------------
async def test_tainted_caller_is_refused_outright_not_sent_to_approval():
    with pytest.raises(Denied, match="third-party"):
        await propose(caller(untrusted=True), {"router.footer": False})
    assert not db.q("SELECT 1 FROM approvals")
    assert audit("denied")


@pytest.mark.parametrize("name,kind", [("job:daily", "job"), ("watch:w1", "job"), ("approval:abc", "job"),
                                       ("watch:w2", "remote"), ("job:x", "remote")])
async def test_jobs_watches_and_approval_followups_are_refused(name, kind):
    with pytest.raises(Denied, match="owner started"):
        await propose(caller(name=name, kind=kind), {"router.footer": False})


async def test_never_auto_approved_even_for_cli_or_auto_approve_list():
    s = config.load()
    s.auto_approve = [SET]
    config.save(s)
    r = await propose(caller(), {"router.footer": False})
    assert r["status"] == "pending_approval"
    from pmjc import auth
    a = auth.admin("cli")
    r2 = await executor.call_tool(a, SET, {"changes": {"front_door.stream_progress": False}, "reason": "x"})
    assert r2["status"] == "pending_approval"
    assert yaml.safe_load(config.path("settings.yaml").read_text())["router"]["footer"] is True


async def test_model_cannot_smuggle_hidden_args():
    r = await executor.call_tool(caller(), SET, {"changes": {"router.footer": False}, "reason": "r",
                                                 "_base_hash": "deadbeef", "_old": {"router.footer": "lie"},
                                                 "_risk": ["off-lower"], "_surface": "forged"})
    a = stored(r["approval_id"])
    assert a["_base_hash"] == settings_edit.file_hash() and a["_old"] == {"router.footer": True}
    assert a["_surface"] == "v1"


async def test_rate_limit_and_cooldown_after_deny():
    c = caller(thread="t1")
    ids = []
    for p in ("router.footer", "front_door.stream_progress", "router.sticky"):
        r = await propose(c, {p: False})
        ids.append(r["approval_id"])
    with pytest.raises(Denied, match="per hour"):
        await propose(c, {"approval_wait_seconds": 20})
    # another caller is unaffected
    assert (await propose(caller(name="other"), {"approval_wait_seconds": 20}))["status"] == "pending_approval"
    # an older proposal no longer counts
    db.x("UPDATE approvals SET created=? WHERE id=?", time.time() - 7200, ids[0])
    assert (await propose(c, {"approval_wait_seconds": 25}))["status"] == "pending_approval"


async def test_cooldown_per_thread_after_a_deny():
    c = caller(thread="t9")
    r = await propose(c, {"router.footer": False})
    await executor.decide(r["approval_id"], False, "tester")
    with pytest.raises(Denied, match="30 minutes"):
        await propose(c, {"front_door.stream_progress": False})
    assert (await propose(caller(thread="t10"), {"front_door.stream_progress": False}))["status"] == "pending_approval"
    db.x("UPDATE approvals SET decided=? WHERE id=?", time.time() - 3600, r["approval_id"])
    assert (await propose(c, {"front_door.stream_progress": False}))["status"] == "pending_approval"


# --- applying: compare-and-swap, patching, backup, revert ---------------------------------------------------------
COMMENTED = """# my settings, hand edited
router:
  # keep answers short
  footer: true   # footer on
  sticky: true
approval_wait_seconds: 50  # phones time out at 60
monitoring_enabled: true
"""


async def test_apply_patches_only_changed_keys_keeps_comments_and_writes_no_defaults(home):
    write_yaml(COMMENTED)
    r = await propose(changes={"router.footer": False, "monitoring_enabled": False})
    res = await approve(r["approval_id"])
    assert res["status"] == "done" and res["result"]["applied"]
    text = config.path("settings.yaml").read_text()
    assert "# my settings, hand edited" in text and "# keep answers short" in text and "# phones time out at 60" in text
    data = yaml.safe_load(text)
    assert data["router"]["footer"] is False and data["monitoring_enabled"] is False
    assert set(data) == {"router", "approval_wait_seconds", "monitoring_enabled"}  # no defaults frozen into the file
    assert set(data["router"]) == {"footer", "sticky"}
    assert config.load().router.footer is False  # visible to the next config.load() in any process
    a = audit("settings_applied")[0]
    assert "base=" in a["detail"] and "by=tester" in a["detail"] and "surface=v1" in a["detail"]
    assert {x["detail"] for x in audit("settings_diff")} == {"router.footer: true -> false", "monitoring_enabled: true -> false"}


async def test_provider_patch_materialises_the_routing_list_once(home):
    write_yaml("# only comments\n")
    r = await propose(changes={"routing[anthropic-api].monthly_budget_usd": 2.5})
    res = await approve(r["approval_id"])
    assert res["status"] == "done"
    s = config.load()
    assert next(p for p in s.routing if p.id == "anthropic-api").monthly_budget_usd == 2.5
    assert [p.id for p in s.routing] == [p.id for p in config.default_providers()]


async def test_stale_write_refuses_when_the_file_changed_between_propose_and_approve(home):
    write_yaml(COMMENTED)
    r = await propose(changes={"router.footer": False})
    write_yaml(COMMENTED + "front_door:\n  history_turns: 2\n")  # someone edits the file
    res = await approve(r["approval_id"])
    assert res["status"] == "failed" and "propose again" in res["error"]
    assert yaml.safe_load(config.path("settings.yaml").read_text())["router"]["footer"] is True
    assert not settings_edit.list_backups()


async def test_tampered_base_hash_in_approval_args_is_caught_by_the_approval_hash(home):
    r = await propose(changes={"router.footer": False})
    a = stored(r["approval_id"])
    a["_base_hash"] = "0" * 64
    db.x("UPDATE approvals SET args=? WHERE id=?", json.dumps(a), r["approval_id"])
    with pytest.raises(Denied, match="payload changed"):
        await approve(r["approval_id"])


async def test_backup_then_revert_restores_and_keeps_the_last_20(home):
    write_yaml(COMMENTED)
    r = await propose(changes={"router.footer": False})
    bid = (await approve(r["approval_id"]))["result"]["backup_id"]
    assert bid in [b["backup_id"] for b in settings_edit.list_backups()]
    assert (home / "settings-backups" / f"{bid}.yaml").read_text() == COMMENTED
    r2 = await executor.call_tool(caller(), REVERT, {"backup_id": bid})
    assert stored(r2["approval_id"])["_changes"] == {"router.footer": True}
    res = await approve(r2["approval_id"])
    assert res["status"] == "done"
    assert config.load().router.footer is True
    for i in range(25):  # pruning
        settings_edit._backup(b"x: %d\n" % i, "%08x" % i)
    assert len(settings_edit.list_backups()) == settings_edit.KEEP_BACKUPS


async def test_revert_is_refused_when_a_denied_path_differs(home):
    write_yaml(COMMENTED)
    r = await propose(changes={"router.footer": False})
    bid = (await approve(r["approval_id"]))["result"]["backup_id"]
    cur = config.load()
    cur.auto_approve = ["pmjc_shell_run"]  # the owner later changes a denied path in the dashboard
    config.save(cur)
    with pytest.raises(Denied, match="dashboard"):
        await executor.call_tool(caller(), REVERT, {"backup_id": bid})
    # and a backup that differs on a denied path in the other direction
    (home / "settings-backups" / f"{bid}.yaml").write_text("port: 9999\nrouter:\n  footer: true\n")
    with pytest.raises(Denied, match="dashboard"):
        await executor.call_tool(caller(), REVERT, {"backup_id": bid})


async def test_revert_never_raises_a_budget(home):
    write_yaml("routing:\n- {id: p, kind: openai_compat, base_url: 'http://127.0.0.1:1/v1', trust: local, monthly_budget_usd: 9}\n")
    r = await propose(changes={"routing[p].monthly_budget_usd": 3})
    bid = (await approve(r["approval_id"]))["result"]["backup_id"]
    with pytest.raises(Denied, match="raised"):
        await executor.call_tool(caller(), REVERT, {"backup_id": bid})


async def test_revert_rejects_bad_backup_ids():
    for b in ("../../etc/passwd", "settings-1", "settings-20200101-000000-zzzzzzzz", ""):
        with pytest.raises(Denied):
            await executor.call_tool(caller(), REVERT, {"backup_id": b})


async def test_invariants_block_a_result_that_drops_every_provider(home):
    write_yaml("routing:\n- {id: only, kind: openai_compat, base_url: 'http://127.0.0.1:1/v1', trust: local}\n")
    r = await propose(changes={"routing[only].enabled": False})
    res = await approve(r["approval_id"])
    assert res["status"] == "failed" and "no model provider" in res["error"]
    assert yaml.safe_load(config.path("settings.yaml").read_text())["routing"][0].get("enabled", True) is True


def test_lock_is_released_and_stale_lock_is_taken_over(home):
    lf = home / "settings.yaml.lock"
    lf.write_text("")
    import os
    old = time.time() - 600
    os.utime(lf, (old, old))
    with config.file_lock(timeout=1):
        assert lf.exists()
    assert not lf.exists()


def test_a_held_lock_blocks_a_second_writer(home):
    lf = home / "settings.yaml.lock"
    lf.write_text("")
    with pytest.raises(TimeoutError):
        with config.file_lock(timeout=0.2):
            pass
    lf.unlink()


def test_backups_are_unreadable_through_the_files_connector(home):
    from pmjc.connectors.local import SECRET_GLOBS
    import fnmatch
    for part in ("settings-backups", "settings.yaml", "settings.yaml.lock"):
        assert any(fnmatch.fnmatch(part, g) for g in SECRET_GLOBS)


# --- get ---------------------------------------------------------------------------------------------------------
async def test_get_returns_only_allowlisted_paths_and_a_provider_summary():
    r = await executor.call_tool(caller(), "pmjc_settings_get", {})
    paths = set(r["settings"])
    assert "router.gpu_busy_action" in paths and "routing[anthropic-api].monthly_budget_usd" in paths
    assert not any(settings_edit.hard_denied(p) for p in paths)
    blob = json.dumps({k: v for k, v in r.items() if k != "note"})
    for leak in ("base_url", "credential", "anthropic.api_key", "127.0.0.1", "public_url", "allowed_hosts"):
        assert leak not in blob
    assert {"id", "kind", "trust", "enabled"} == set(r["providers"][0])
    sub = await executor.call_tool(caller(), "pmjc_settings_get", {"path": "router"})
    assert sub["settings"] and all(p.startswith("router.") for p in sub["settings"])
    assert "error" in await executor.call_tool(caller(), "pmjc_settings_get", {"path": "auto_approve"})


# --- profiles ----------------------------------------------------------------------------------------------------
GAMING = """# profiles are the owner's
profiles:
  gaming:
    router.gpu_busy_action: ask
    routing[lmstudio-local].enabled: false
    router.gpu_busy_jobs: wait
  broken:
    auto_approve: [pmjc_shell_run]
  badvalue:
    router.stay_tool_calls: 99
router:
  gpu_busy_action: local
"""


async def test_profile_switch_applies_in_either_direction_and_lists_active(home):
    write_yaml(GAMING)
    g = await executor.call_tool(caller(), "pmjc_settings_get", {})
    by = {p["name"]: p for p in g["profiles"]}
    assert by["gaming"]["valid"] and not by["gaming"]["active"] and g["active_profile"] is None
    assert not by["broken"]["valid"] and not by["badvalue"]["valid"]
    r = await propose(tool=PROFILE, name="gaming")
    a = stored(r["approval_id"])
    assert a["_changes"] == {"router.gpu_busy_action": "ask", "routing[lmstudio-local].enabled": False}
    assert (await approve(r["approval_id"]))["status"] == "done"
    s = config.load()
    assert s.router.gpu_busy_action == "ask" and not s.routing[0].enabled
    assert "# profiles are the owner's" in config.path("settings.yaml").read_text()
    assert (await executor.call_tool(caller(), "pmjc_settings_get", {}))["active_profile"] == "gaming"
    with pytest.raises(Denied, match="already matches"):
        await propose(tool=PROFILE, name="gaming")
    # profiles may raise things chat itself could not (owner-authored)
    write_yaml("profiles:\n  travel:\n    monitoring_enabled: true\n")
    r = await propose(tool=PROFILE, name="travel")
    assert (await approve(r["approval_id"]))["status"] == "done" and config.load().monitoring_enabled


async def test_bad_profiles_and_missing_profiles_are_refused(home):
    write_yaml(GAMING)
    for name in ("broken", "badvalue", "nope"):
        with pytest.raises(Denied):
            await propose(tool=PROFILE, name=name)
    write_yaml("profiles:\n  p:\n    profiles.x: 1\n    port: 1\n")
    with pytest.raises(Denied, match="can't be changed"):
        await propose(tool=PROFILE, name="p")
    write_yaml("profiles:\n  p:\n    routing[anthropic-api].monthly_budget_usd: null\n")
    with pytest.raises(Denied, match="never null"):
        await propose(tool=PROFILE, name="p")


async def test_profile_switch_gates_callers_like_set(home):
    write_yaml(GAMING)
    with pytest.raises(Denied):
        await propose(caller(untrusted=True), tool=PROFILE, name="gaming")
    with pytest.raises(Denied):
        await propose(caller(name="job:x"), tool=PROFILE, name="gaming")


async def test_profiles_cannot_be_written_from_chat():
    with pytest.raises(Denied):
        await propose(changes={"profiles": {"x": {"port": 1}}})
    with pytest.raises(Denied):
        await propose(changes={"profiles.gaming": {}})


# --- approval rendering ------------------------------------------------------------------------------------------
EVIL = "@everyone <@123> [click](http://evil.example) **bold** `tick` \u202etxt\u200b>quote #h"


async def test_discord_text_escapes_markdown_mentions_and_invisible_characters():
    r = await propose(changes={"router.footer_surfaces": ["discord"]}, reason=EVIL)
    row = db.q("SELECT tool,args,reason,tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    from pmjc import discord_door
    text = discord_door.approval_text(row)
    assert "@everyone" not in text and "<@123>" not in text and "\u202e" not in text and "\u200b" not in text
    assert "[click](http" not in text and "**bold**" not in text and "`tick`" not in text
    assert text.splitlines()[0].startswith("**[OFF-LOWER] Settings change**")
    assert "router.footer_surfaces" in text and "−v1" in text and "−cli" not in text.replace("−cli", "x") or "−v1" in text
    assert text.splitlines()[-1].startswith("> model's claim:")
    assert "```" not in text


async def test_diff_lines_show_old_to_new_and_lists_as_added_removed():
    r = await propose(changes={"router.gpu_busy_action": "local"})
    row = db.q("SELECT tool,args,reason,tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    text = settings_edit.discord_text(row)
    assert "`ask` → `local`" in text and "[ROUTING]" in text
    s = config.load()
    s.disabled_connectors = ["weather"]
    config.save(s)
    r = await propose(changes={"disabled_connectors": ["weather", "places"]})
    row = db.q("SELECT tool,args,reason,tainted FROM approvals WHERE id=?", r["approval_id"])[0]
    assert "+places" in settings_edit.discord_text(row)


async def test_value_injection_in_provider_ids_and_strings_is_refused_or_escaped():
    with pytest.raises(Denied):
        await propose(changes={"routing[x`@everyone].enabled": False})
    assert "@" not in settings_edit.discord_text({"tool": SET, "reason": "",
        "args": json.dumps({"changes": {"router.gpu_busy_action": "@everyone"}, "_old": {"router.gpu_busy_action": "ask"},
                            "_risk": ["routing"], "reason": "", "_surface": "v1"})})


async def test_two_step_buttons_for_routing_and_cost_but_not_off_lower():
    from pmjc import discord_door
    r = await propose(changes={"router.gpu_busy_action": "local"})
    aid = r["approval_id"]
    first = discord_door.approval_buttons(aid)[0]["components"]
    assert [b["label"] for b in first] == ["Review", "Deny"] and first[0]["custom_id"].endswith(f"r:{aid}")
    second = discord_door.approval_buttons(aid, reviewed=True)[0]["components"]
    assert [b["label"] for b in second] == ["Approve", "Deny"]
    r2 = await propose(changes={"router.footer": False})
    assert [b["label"] for b in discord_door.approval_buttons(r2["approval_id"])[0]["components"]] == ["Approve", "Deny"]
    r3 = await propose(caller(name="c3"), {"research.max_turns": 3})  # cost class
    assert discord_door.approval_buttons(r3["approval_id"])[0]["components"][0]["label"] == "Review"


async def test_discord_review_click_edits_the_message_then_approve_applies(monkeypatch):
    from pmjc import discord_door
    calls = []

    async def api(method, path, body=None):
        calls.append((method, path, body))
        return {"id": "m1"}
    cfg = config.DiscordDoorConfig(enabled=True, owner_user_ids=["111"])
    door = discord_door.Door(cfg, api, bot_id="9")
    r = await propose(changes={"router.gpu_busy_action": "local"})
    aid = r["approval_id"]
    row = db.q("SELECT tool,args,reason,tainted FROM approvals WHERE id=?", aid)[0]
    content = discord_door.approval_text(row)
    base = {"id": "i1", "token": "tok", "type": 3, "member": {"user": {"id": "111"}}, "channel_id": "c",
            "message": {"content": content}}

    def click(kind, content=content, iid="i1"):
        return {**base, "id": iid, "message": {"content": content}, "data": {"custom_id": f"pmjc:{kind}:{aid}"}}
    await door.on_interaction(click("a"))  # Approve before Review: refused, nothing runs
    assert db.q("SELECT status FROM approvals WHERE id=?", aid)[0]["status"] == "pending"
    assert calls[-1][2]["data"]["content"].startswith("Press Review first")
    await door.on_interaction(click("r", iid="i2"))
    edit = calls[-1][2]["data"]
    assert [b["label"] for b in edit["components"][0]["components"]] == ["Approve", "Deny"]
    assert settings_edit.REVIEWED_MARK in edit["content"] and edit["allowed_mentions"] == {"parse": []}
    await door.on_interaction(click("a", content=edit["content"], iid="i3"))
    await door.drain()
    assert db.q("SELECT status FROM approvals WHERE id=?", aid)[0]["status"] == "done"
    assert config.load().router.gpu_busy_action == "local"
    # a stranger can't review
    r2 = await propose(caller(name="c2"), {"research.max_turns": 3})
    await door.on_interaction({**base, "id": "i9", "member": {"user": {"id": "222"}},
                               "data": {"custom_id": f"pmjc:r:{r2['approval_id']}"}})
    assert "Only the owner" in calls[-1][2]["data"]["content"]


async def test_link_page_shows_the_diff_and_needs_a_review_step():
    from fastapi.testclient import TestClient
    from pmjc import auth, server
    r = await propose(changes={"router.gpu_busy_action": "local"}, reason=EVIL)
    aid = r["approval_id"]
    sig = auth.sign("approve:" + aid)
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    page = c.get(f"/approve/{aid}", params={"sig": sig}).text
    assert "router.gpu_busy_action" in page and "ask → local" in page and "[ROUTING]" in page
    assert "<form" not in page and "I reviewed it" in page  # step one: no approve button yet
    assert "@everyone" in page and "<script" not in page and "[click](" in page  # HTML-escaped, shown verbatim
    refused = c.post(f"/approve/{aid}", data={"sig": sig, "deny": "0", "reviewed": "0"})
    assert refused.status_code == 400
    assert db.q("SELECT status FROM approvals WHERE id=?", aid)[0]["status"] == "pending"
    page2 = c.get(f"/approve/{aid}", params={"sig": sig, "review": 1}).text
    assert "<form" in page2 and "Approve" in page2
    ok = c.post(f"/approve/{aid}", data={"sig": sig, "deny": "0", "reviewed": "1"})
    assert ok.status_code == 200
    assert config.load().router.gpu_busy_action == "local"


async def test_dashboard_api_carries_a_view_for_settings_approvals():
    from fastapi.testclient import TestClient
    from pmjc import auth, server
    r = await propose(changes={"router.footer": False})
    tok = auth.create_token("dash", ["admin"])
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:8770")
    rows = c.get("/api/approvals", headers={"Authorization": f"Bearer {tok}"}).json()
    v = next(x for x in rows if x["id"] == r["approval_id"])["view"]
    assert v["badge"] == "OFF-LOWER" and v["lines"][0] == {"path": "router.footer", "text": "true → false"}
