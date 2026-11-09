"""Owner chat commands /settings, /set, /profile, /revert on /v1: answered by pmjc, no model run."""
from __future__ import annotations

from test_context import home  # noqa: F401
from test_openai_door import api, body  # noqa: F401
from test_chat_commands import say


def cfg():
    from pmjc import config
    return config.load()


def test_settings_lists_values_profiles_and_needs_no_model(api):
    c, h, calls = api
    from pmjc import config
    s = config.load()
    s.profiles = {"gaming": {"router.gpu_busy_action": "local"}}
    config.save(s)
    out = say(c, h, "/settings")
    assert "router.gpu_busy_action" in out and "gaming" in out and "Providers" in out
    sub = say(c, h, "/settings router")
    assert "router." in sub and "monitoring_enabled" not in sub
    assert "can't be read" in say(c, h, "/settings auto_approve")
    assert calls == []


def test_set_off_lower_applies_at_once_and_audits_as_owner_command(api):
    c, h, calls = api
    out = say(c, h, "/set commands.wait_seconds=5 front_door.stream_progress=false")
    assert "Applied" in out and "commands.wait_seconds" in out
    s = cfg()
    assert s.commands.wait_seconds == 5 and s.front_door.stream_progress is False
    from pmjc import db
    assert "owner command" in db.q("SELECT detail FROM audit WHERE status='settings_applied'")[0]["detail"]
    assert not say(c, h, "/set front_door.history_turns 2").startswith("**Applied**")  # cost class: approval
    assert calls == []


def test_set_single_path_yaml_values_and_lists(api):
    c, h, _ = api
    assert "Applied" in say(c, h, "/set router.footer_surfaces [discord]")
    assert cfg().router.footer_surfaces == ["discord"]


def test_routing_and_cost_changes_reply_with_an_approval_not_applied(api):
    c, h, _ = api
    for cmd, path in (("/set router.gpu_busy_action local", "router.gpu_busy_action"),
                      ("/set research.max_turns 3", "research.max_turns")):
        out = say(c, h, cmd)
        assert "Approve](" in out and "Deny](" in out and path in out and "Nothing changes" in out
    assert cfg().router.gpu_busy_action == "ask" and cfg().research.max_turns == 8
    from pmjc import db
    assert len(db.q("SELECT id FROM approvals WHERE status='pending' AND tool='pmjc_settings_set'")) == 2


def test_set_refusals(api):
    c, h, _ = api
    for cmd in ("/set auto_approve [pmjc_shell_run]", "/set port=1", "/set routing[anthropic-api].base_url=x",
                "/set monitoring_enabled true", "/set research.max_turns 50", "/set a=1", "/set", "/set front_door.stream_progress",
                "/set front_door.stream_progress=false router.sticky=false front_door.stream_progress=false commands.wait_seconds=3"):
        assert say(c, h, cmd).startswith("Refused"), cmd
    assert cfg().front_door.stream_progress is True


def test_profile_lists_and_switch_asks_for_approval(api):
    c, h, _ = api
    assert "none" in say(c, h, "/profile")
    from pmjc import config
    s = config.load()
    s.profiles = {"gaming": {"router.gpu_busy_action": "local", "monitoring_enabled": True}, "bad": {"port": 1}}
    config.save(s)
    listing = say(c, h, "/profile")
    assert "gaming" in listing and "invalid" in listing
    out = say(c, h, "/profile gaming")
    assert "Approve](" in out and "Switch profile" in out
    assert cfg().router.gpu_busy_action == "ask"
    assert say(c, h, "/profile bad").startswith("Refused")
    assert say(c, h, "/profile nope").startswith("Refused")


def test_revert_lists_backups_and_asks_for_approval(api):
    c, h, _ = api
    assert say(c, h, "/revert") == "No backups yet."
    say(c, h, "/set front_door.stream_progress=false")
    listing = say(c, h, "/revert")
    bid = listing.split("`")[3]
    assert bid.startswith("settings-")
    out = say(c, h, f"/revert {bid}")
    assert "Approve](" in out and "stream_progress" in out
    assert cfg().front_door.stream_progress is False
    assert say(c, h, "/revert ../../x").startswith("Refused")


def test_token_without_actions_scope_is_refused(api):
    from pmjc import auth
    c, _, _ = api
    h = {"Authorization": "Bearer " + auth.create_token("ro", ["sources"])}
    assert "actions" in say(c, h, "/set front_door.stream_progress=false")
    assert cfg().front_door.stream_progress is True
