"""Browser connector: links, extraction, allowed domains, and owner-declared reads after taint."""
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


class FakePage:
    """Stands in for a Playwright page; eval_on_selector_all returns what the in-page JS would."""
    url = "https://shop.example.com/list?q=1"

    def __init__(self):
        self.closed = False

    async def inner_text(self, sel, timeout=0):
        return "Hello list"

    async def title(self):
        return "List"

    async def eval_on_selector_all(self, selector, js, arg=None):
        if selector == "a[href]":
            return [{"text": " Item  one ", "href": "/item/1"}, {"text": "dup", "href": "/item/1#frag"},
                    {"text": "Two", "href": "https://other.org/x"}, {"text": "skip", "href": "javascript:void(0)"},
                    {"text": "top", "href": "#top"}, {"text": "Three", "href": "item/3"}]
        assert selector == "li.row" and set(arg) == {"title", "price", "link", "id"}
        assert arg["link"] == ["a", "href"] and arg["id"] == ["", "data-id"] and arg["title"] == ["h3", None]
        return [{"title": " Chair \n big", "price": "$5", "link": "/item/1", "id": "7"},
                {"title": "Desk", "price": None, "link": "https://shop.example.com/item/2", "id": "8"},
                {"title": "Lamp", "price": "$1", "link": "/item/3", "id": "9"}]

    async def close(self):
        self.closed = True


def _browser(monkeypatch):
    from pmjc.connectors.browser import Browser
    b = Browser()
    pages = []

    async def fake_open(url, wait):
        p = FakePage()
        pages.append(p)
        return p
    monkeypatch.setattr(b, "_open", fake_open)
    return b, pages


async def test_read_returns_absolute_deduped_capped_links(monkeypatch):
    b, pages = _browser(monkeypatch)
    r = await b.browser_read("https://shop.example.com/list")
    hrefs = [x["href"] for x in r["links"]]
    assert hrefs == ["https://shop.example.com/item/1", "https://other.org/x", "https://shop.example.com/item/3"]
    assert r["links"][0]["text"] == "Item one"
    r = await b.browser_read("https://shop.example.com/list", max_links=2)
    assert len(r["links"]) == 2
    assert (await b.browser_read("https://shop.example.com/list", max_links=0))["links"] == []
    assert all(p.closed for p in pages)


async def test_extract_rows_and_attributes(monkeypatch):
    b, pages = _browser(monkeypatch)
    fields = {"title": "h3", "price": ".price", "link": "a@href", "id": "@data-id"}
    r = await b.browser_extract("https://shop.example.com/list", "li.row", fields, max_items=2)
    assert r["count"] == 2
    assert r["rows"][0] == {"title": "Chair big", "price": "$5", "link": "https://shop.example.com/item/1", "id": "7"}
    assert r["rows"][1]["price"] is None and r["rows"][1]["link"] == "https://shop.example.com/item/2"
    assert pages[0].closed


async def test_extract_and_read_enforce_allowed_domains(monkeypatch):
    from pmjc.connector import ConnectorError
    b, _ = _browser(monkeypatch)
    with pytest.raises(ConnectorError):
        await b.browser_extract("https://evil.org/x", "li", {"t": "h3"})
    with pytest.raises(ConnectorError):
        await b.browser_read("https://evil.org/x")
    with pytest.raises(ConnectorError):
        await b.browser_extract("https://example.com/x", "li", {})


def test_channel_and_headless_options_default_to_current_behaviour():
    from pmjc.connectors.browser import Browser
    b = Browser()
    assert b.option("channel", "") == "" and b.option("headless", True) is True


# --- declared reads after taint ---------------------------------------------------------------------------
def _tainted(**kw):
    from pmjc import auth
    c = auth.Caller("job:x", "job", {"sources"}, **kw)
    c.untrusted = True
    return c


@pytest.fixture
def browser_tools(monkeypatch):
    from pmjc import registry
    from pmjc.connectors import browser as bmod
    monkeypatch.setattr(bmod.Browser, "configured", lambda self: True)
    reg = registry.reload()
    b = reg.connectors["browser"]

    async def fake_open(url, wait):
        return FakePage()
    monkeypatch.setattr(b, "_open", fake_open)
    return reg


async def test_declared_url_allowed_after_taint_but_others_denied(browser_tools):
    from pmjc import executor
    c = _tainted(read_urls=("https://shop.example.com/list",), read_sites=("example.org",))
    r = await executor.call_tool(c, "browser_read", {"url": "https://shop.example.com/list"})
    assert r["links"]
    r = await executor.call_tool(c, "browser_extract", {"url": "https://shop.example.com/list", "selector": "li.row",
                                                        "fields": {"title": "h3", "price": ".price", "link": "a@href", "id": "@data-id"}})
    assert r["count"] == 3
    for url in ("https://shop.example.com/list?x=secret", "https://shop.example.com/other", "https://notexample.org/"):
        with pytest.raises(executor.Denied):
            await executor.call_tool(c, "browser_read", {"url": url})
    assert c.untrusted  # still tainted


async def test_declared_site_matches_subdomains_only(browser_tools):
    from pmjc import executor
    assert executor.declared_read(_tainted(read_sites=("example.com",)), "browser_read", {"url": "https://a.example.com/p"})
    assert not executor.declared_read(_tainted(read_sites=("example.com",)), "browser_read", {"url": "https://badexample.com/p"})
    assert not executor.declared_read(_tainted(read_sites=("example.com",)), "browser_read", {"url": "ftp://example.com/p"})
    # other egress tools are never exempt, and an empty list exempts nothing
    assert not executor.declared_read(_tainted(read_sites=("example.com",)), "ssh_run", {"url": "https://example.com/"})
    assert not executor.declared_read(_tainted(), "browser_read", {"url": "https://example.com/"})


async def test_undeclared_tainted_read_denied(browser_tools):
    from pmjc import executor
    with pytest.raises(executor.Denied):
        await executor.call_tool(_tainted(), "browser_read", {"url": "https://shop.example.com/list"})


def test_models_cannot_declare_reads(home):
    from pmjc import auth, jobs
    from pmjc.jobs import Job
    j = Job(name="t1", prompt="p", read_urls=["https://example.com/a"], read_sites=["example.com"])
    f = jobs.save_job(j, created_by=auth.Caller("agent", "remote", {"sources", "jobs"}))
    assert "read_urls" in f.read_text() and jobs.load_jobs()["t1"].read_urls == []
    assert jobs.load_jobs()["t1"].read_sites == []
    # the owner (admin) can declare them, and the engine hands them to the run's caller
    jobs.save_job(Job(name="t2", prompt="p", read_urls=["https://example.com/a"], read_sites=["example.com"]),
                  created_by=auth.admin())
    assert jobs.load_jobs()["t2"].read_urls == ["https://example.com/a"]


async def test_watch_spec_reads_only_from_owner(home):
    from pmjc import auth, jobs, registry
    registry.reload()
    e = jobs.Engine()
    spec = {"type": "time", "after_seconds": 600, "respond": "raw",
            "read_urls": ["https://example.com/a"], "read_sites": ["example.com"]}
    w = e.watches.start(auth.Caller("agent", "remote", {"sources", "jobs"}), "x", dict(spec))
    assert "read_urls" not in w["spec"] and "read_sites" not in w["spec"]
    w2 = e.watches.start(auth.admin(), "y", dict(spec))
    c = e.watches._caller({"id": w2["id"], "scopes": ["sources"], "spec": w2["spec"]})
    assert c.read_urls == ("https://example.com/a",) and c.read_sites == ("example.com",)
