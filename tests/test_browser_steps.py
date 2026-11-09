"""browser_browse (read) and browser_act (act): step validation, approval, safety guard, domain checks."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PMJC_HOME", str(tmp_path))
    monkeypatch.setenv("PMJC_MASTER_KEY", "k")
    from pmjc import config, creds, db, registry
    db._local.c = None
    creds._store = None
    registry._reg = None
    s = config.load(); s.credential_backend = "file"
    s.connectors["browser"] = {"allowed_domains": ["example.com"]}
    config.save(s)
    yield tmp_path
    db._local.c = None


class StepPage:
    """Stands in for a Playwright page; records steps. The click guard result is `click_result` (the real in-page JS
    is covered by the optional headless test below)."""
    url = "https://shop.example.com/list"
    click_result = {"ok": True}

    def __init__(self):
        self.closed = False
        self.calls = []

    async def inner_text(self, sel, timeout=0):
        return "Hello list"

    async def title(self):
        return "List"

    async def eval_on_selector_all(self, selector, js, arg=None):
        return [{"text": "One", "href": "/item/1"}]

    async def evaluate(self, js, arg=None):
        self.calls.append(("evaluate", arg))
        return self.click_result if isinstance(arg, list) else None

    async def wait_for_selector(self, sel, timeout=0):
        self.calls.append(("wait_for", sel))

    async def fill(self, sel, text, timeout=0):
        self.calls.append(("fill", sel, text))

    @property
    def keyboard(self):
        page = self

        class K:
            async def press(self, key):
                page.calls.append(("press", key))
        return K()

    async def close(self):
        self.closed = True


@pytest.fixture
def step_tools(monkeypatch):
    from pmjc import registry
    from pmjc.connectors import browser as bmod
    monkeypatch.setattr(bmod.Browser, "configured", lambda self: True)
    StepPage.click_result = {"ok": True}
    reg = registry.reload()
    b = reg.connectors["browser"]
    pages = []

    async def fake_open(url, wait):
        p = StepPage()
        pages.append(p)
        return p
    monkeypatch.setattr(b, "_open", fake_open)
    return b, reg, pages


async def test_interaction_tool_flags(step_tools):
    _b, reg, _p = step_tools
    act, browse = reg.tool("browser_act"), reg.tool("browser_browse")
    assert act.scope == "act" and act.egress and act.untrusted_output
    assert browse.scope == "read" and browse.egress and browse.untrusted_output


async def test_browser_act_needs_approval_and_shows_steps(step_tools):
    from pmjc import auth, db, executor
    c = auth.Caller("agent", "remote", {"sources", "actions"})
    steps = [{"type": {"selector": "#q", "text": "chair"}}, {"press": "Enter"}]
    r = await executor.call_tool(c, "browser_act", {"url": "https://shop.example.com/", "steps": steps})
    assert r["status"] == "pending_approval"
    args = db.q("SELECT args FROM approvals WHERE id=?", r["approval_id"])[0]["args"]
    assert "chair" in args and "Enter" in args
    done = await executor.decide(r["approval_id"], True, "test")
    assert done["status"] == "done" and done["result"]["steps_run"] == 2
    assert done["result"]["links"] and done["result"]["url"].startswith("https://shop.example.com")


async def test_browser_act_runs_all_step_kinds(step_tools):
    b, _reg, pages = step_tools
    r = await b.browser_act("https://shop.example.com/", [
        {"scroll": 2}, {"wait": 0}, {"wait_for": ".row"}, {"click": "text=More"},
        {"type": {"selector": "#q", "text": "x"}}, {"press": "Enter"}])
    assert r["steps_run"] == 6
    assert [c[0] for c in pages[0].calls] == ["evaluate", "wait_for", "evaluate", "fill", "press"]
    assert pages[0].calls[2][1] == ["text=More", False]  # act: guard off
    assert pages[0].closed


async def test_browse_refuses_type_and_press(step_tools):
    from pmjc.connector import ConnectorError
    b, _reg, pages = step_tools
    for bad in ({"type": {"selector": "#q", "text": "x"}}, {"press": "Enter"}, {"submit": "#f"}):
        with pytest.raises(ConnectorError):
            await b.browser_browse("https://shop.example.com/", [{"scroll": 1}, bad])
    assert not pages  # refused before opening anything


async def test_browse_click_uses_safety_guard_and_reports_refusals(step_tools):
    from pmjc.connector import ConnectorError
    b, _reg, pages = step_tools
    r = await b.browser_browse("https://shop.example.com/", [{"click": "button.more"}])
    assert pages[0].calls == [("evaluate", ["button.more", True])] and r["steps_run"] == 1
    for why in ("refused: inside a form", "refused: submit control", "refused: link to another domain"):
        StepPage.click_result = {"error": why}
        with pytest.raises(ConnectorError, match=why):
            await b.browser_browse("https://shop.example.com/", [{"click": "x"}])
    assert all(p.closed for p in pages)


async def test_step_cap_and_shape(step_tools):
    from pmjc.connector import ConnectorError
    b, _reg, _p = step_tools
    await b.browser_browse("https://shop.example.com/", [{"wait": 0}] * 10)
    with pytest.raises(ConnectorError, match="at most 10"):
        await b.browser_browse("https://shop.example.com/", [{"wait": 0}] * 11)
    for bad in ([], "scroll", [{"scroll": 1, "wait": 1}], [{"scroll": "x"}], [{"click": ""}]):
        with pytest.raises(ConnectorError):
            await b.browser_act("https://shop.example.com/", bad)


async def test_domains_enforced_before_and_after_navigation(step_tools, monkeypatch):
    from pmjc.connector import ConnectorError
    b, _reg, _p = step_tools
    for tool in (b.browser_browse, b.browser_act):
        with pytest.raises(ConnectorError, match="allowed_domains"):
            await tool("https://evil.org/", [{"scroll": 1}])

    class Hopper(StepPage):
        async def evaluate(self, js, arg=None):
            self.url = "https://evil.org/landed"  # a click navigated off-site
            return {"ok": True}

    async def fake_open(url, wait):
        return Hopper()
    monkeypatch.setattr(b, "_open", fake_open)
    with pytest.raises(ConnectorError, match="allowed_domains"):
        await b.browser_act("https://shop.example.com/", [{"click": "a"}, {"scroll": 1}])


_GUARD_HTML = """<body><a id=ok href="/more">Show more</a><a id=ext href="https://other.org/x">Out</a>
<form><button id=sub>Go</button></form><button id=exp onclick="this.textContent='opened'">Expand</button>
<input id=free type=submit></body>"""


async def test_real_click_guard_in_headless_page():
    pw = pytest.importorskip("playwright.async_api")
    from pmjc.connectors.browser import _CLICK_JS
    try:
        async with pw.async_playwright() as p:
            br = await p.chromium.launch(headless=True)
            page = await br.new_page()
            await page.set_content(_GUARD_HTML)
            res = {t: await page.evaluate(_CLICK_JS, [t, True]) for t in ("#sub", "#ext", "#free", "#nope")}
            assert "form" in res["#sub"]["error"] and "domain" in res["#ext"]["error"]
            assert "submit" in res["#free"]["error"] and res["#nope"]["error"] == "not found"
            assert (await page.evaluate(_CLICK_JS, ["text=expand", True])) == {"ok": True}
            assert await page.inner_text("#exp") == "opened"
            await br.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            pytest.skip("Chromium not installed")
        raise
