"""Craigslist connector against a mocked transport; never calls Craigslist. The feed layout is unverified."""
from __future__ import annotations

import httpx
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
    s.connectors["craigslist"] = {"areas": {"Springfield": 42}}
    config.save(s)
    yield tmp_path
    db._local.c = None


DECODE = {"minPostedDate": 1700000000, "locations": [[1, "dtown"], [2, "uptown"]]}
ITEMS = [[111, 60, 0, 25, "1:0:0", 0, "old-chair-slug", 0, 0, 0, 0, 0, 0, "abc123", "Old chair"],
         [112, 0, 0, 0, "0:0:0", 0, "free-desk", 0, 0, 0, 0, 0, 0, "def456", "Free desk"],
         "garbage"]


def _make(calls, items=ITEMS):
    from pmjc.connectors.craigslist import Craigslist

    def h(req: httpx.Request):
        calls.append(req)
        return httpx.Response(200, json={"data": {"items": items, "decode": DECODE}})
    c = Craigslist()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(h))
    return c


async def test_tool_flags():
    from pmjc import registry
    t = registry.reload().tool("craigslist_search")
    assert t.scope == "read" and t.egress and t.untrusted_output


async def test_mapping_url_and_params():
    calls = []
    c = _make(calls)
    r = await c.craigslist_search([{"q": "chair", "area": "springfield", "min_price": 5, "max_price": 50, "category": "fua"}])
    rows = r[0]["rows"]
    assert len(rows) == 2 and r[0]["area"] == "springfield"
    assert rows[0]["url"] == "https://www.craigslist.org/view/d/old-chair-slug/abc123"
    assert rows[0]["title"] == "Old chair" and rows[0]["price"] == 25 and rows[0]["location"] == "uptown"
    assert rows[0]["posted"] == "2023-11-14 22:14"
    q = calls[0].url.params
    assert calls[0].url.host == "sapi.craigslist.org" and calls[0].url.path == "/web/v8/postings/search/full"
    assert q["batch"].startswith("42-") and q["query"] == "chair" and q["searchPath"] == "fua"
    assert q["min_price"] == "5" and q["max_price"] == "50"


async def test_default_category_limit_and_numeric_area():
    calls = []
    c = _make(calls)
    r = await c.craigslist_search([{"q": "x", "area": "7", "limit": 1}])
    assert len(r[0]["rows"]) == 1 and calls[0].url.params["searchPath"] == "sss" and calls[0].url.params["batch"].startswith("7-")


async def test_query_cap_and_errors_are_per_query():
    calls = []
    c = _make(calls)
    r = await c.craigslist_search([{"q": "a", "area": "1"}] * 12)
    assert len(r) == 10 and len(calls) == 10
    r = await c.craigslist_search([{"q": "a", "area": "nowhere"}, {"q": "b"}, {"area": "1"}, {"q": "ok", "area": "1"}])
    assert [bool(x.get("error")) for x in r] == [True, True, True, False]
    with pytest.raises(ValueError):
        await c.craigslist_search([])
