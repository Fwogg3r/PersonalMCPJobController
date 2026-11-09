"""eBay Browse connector against a mocked transport; never calls eBay."""
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
    s = config.load(); s.credential_backend = "file"; config.save(s)
    yield tmp_path
    db._local.c = None


def _make(handler, keys=True):
    from pmjc import creds
    from pmjc.connectors.ebay import Ebay
    if keys:
        creds.store().set("ebay.client_id", "app-id")
        creds.store().set("ebay.client_secret", "cert-id")
    c = Ebay()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _server(calls, items=None, details=None):
    def h(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})
        if "/item_summary/search" in req.url.path:
            return httpx.Response(200, json={"total": 2, "itemSummaries": items or []})
        if "/buy/browse/v1/item/" in req.url.path:
            iid = req.url.raw_path.decode().split("?")[0].split("/item/")[1].replace("%7C", "|")
            return httpx.Response(200, json=(details or {}).get(iid, {}))
        return httpx.Response(404)
    return h


ITEMS = [
    {"itemId": "v1|1|0", "title": "Widget A", "price": {"value": "10.50", "currency": "USD"},
     "shippingOptions": [{"shippingCost": {"value": "4.00", "currency": "USD"}}, {"shippingCost": {"value": "2.00", "currency": "USD"}}],
     "condition": "New", "buyingOptions": ["FIXED_PRICE"], "seller": {"feedbackPercentage": "99.5"},
     "itemLocation": {"country": "US", "postalCode": "12345-6789"}, "itemWebUrl": "https://www.ebay.com/itm/1", "image": {"x": 1}},
    {"itemId": "v1|2|0", "title": "Widget B", "price": {"value": "8", "currency": "USD"}, "buyingOptions": ["AUCTION"],
     "itemWebUrl": "https://www.ebay.com/itm/2"},
]


async def test_tool_flags():
    from pmjc import registry
    t = registry.reload().tool("ebay_search")
    assert t.scope == "read" and t.egress and t.untrusted_output


async def test_token_cached_and_headers():
    calls = []
    c = _make(_server(calls, ITEMS))
    await c.ebay_search([{"q": "a"}])
    await c.ebay_search([{"q": "b"}])
    assert sum(r.url.path.endswith("/oauth2/token") for r in calls) == 1
    search = [r for r in calls if "item_summary" in r.url.path][0]
    assert search.headers["authorization"] == "Bearer tok"
    assert search.headers["x-ebay-c-marketplace-id"] == "EBAY_US"
    tok = calls[0]
    assert tok.headers["authorization"].startswith("Basic ") and b"client_credentials" in tok.content
    assert b"api_scope" in tok.content and tok.url.host == "api.ebay.com"


async def test_marketplace_and_sandbox_config():
    from pmjc import config
    s = config.load(); s.connectors["ebay"] = {"marketplace": "EBAY_GB", "sandbox": True}; config.save(s)
    calls = []
    c = _make(_server(calls, ITEMS))
    await c.ebay_search([{"q": "a"}])
    assert all(r.url.host == "api.sandbox.ebay.com" for r in calls)
    assert calls[-1].headers["x-ebay-c-marketplace-id"] == "EBAY_GB"


async def test_filter_building_and_compaction():
    calls = []
    c = _make(_server(calls, ITEMS))
    out = await c.ebay_search([{"q": "widget", "min_price": 5, "max_price": 20, "buying_options": ["fixed_price", "AUCTION"],
                                "conditions": ["new", "USED"], "category_id": 99, "limit": 500}])
    req = [r for r in calls if "item_summary" in r.url.path][0]
    p = dict(req.url.params)
    assert p["filter"] == "price:[5..20],priceCurrency:USD,buyingOptions:{FIXED_PRICE|AUCTION},conditions:{NEW|USED}"
    assert p["q"] == "widget" and p["category_ids"] == "99" and p["limit"] == "50"
    rows = out[0]["rows"]
    assert rows[0] == {"id": "v1|1|0", "title": "Widget A", "price": 10.5, "currency": "USD", "shipping": 2.0,
                       "condition": "New", "buying_options": ["FIXED_PRICE"], "seller_feedback_pct": "99.5",
                       "location": "US/12345", "url": "https://www.ebay.com/itm/1"}
    assert "image" not in rows[0] and rows[1]["price"] == 8.0


async def test_open_ended_price_filter():
    from pmjc.connectors.ebay import build_filter
    assert build_filter({"max_price": 30}, "USD") == "price:[..30],priceCurrency:USD"
    assert build_filter({"min_price": 3}, "USD") == "price:[3..],priceCurrency:USD"
    assert build_filter({}, "USD") == ""
    with pytest.raises(ValueError):
        build_filter({"buying_options": ["NOPE"]}, "USD")


async def test_details_and_min_quantity():
    calls = []
    det = {"v1|1|0": {"estimatedAvailabilities": [{"estimatedAvailableQuantity": 5}],
                          "localizedAspects": [{"name": "Brand", "value": "Acme"}]},
           "v1|2|0": {"estimatedAvailabilities": [{"estimatedAvailableQuantity": 1}]}}
    c = _make(_server(calls, ITEMS, det))
    out = await c.ebay_search([{"q": "w", "min_quantity": 3}])
    rows = out[0]["rows"]
    assert [r["id"] for r in rows] == ["v1|1|0"] and rows[0]["quantity"] == 5 and rows[0]["specifics"] == {"Brand": "Acme"}
    # without details/min_quantity no getItem calls
    calls.clear()
    await c.ebay_search([{"q": "w"}])
    assert not any("/item/" in r.url.path for r in calls)
    out = await c.ebay_search([{"q": "w"}], details=True)
    assert len(out[0]["rows"]) == 2 and out[0]["rows"][1]["quantity"] == 1


async def test_queries_cap_and_error_isolation():
    calls = []
    c = _make(_server(calls, ITEMS))
    qs = [{"q": f"q{i}"} for i in range(15)] 
    qs[2] = {}  # invalid: reported, doesn't sink the call
    out = await c.ebay_search(qs)
    assert len(out) == 10 and "error" in out[2] and out[3]["rows"]
    with pytest.raises(ValueError):
        await c.ebay_search([])


async def test_health_not_configured_and_configured():
    c = _make(_server([]), keys=False)
    h = await c.health()
    assert not h.ok and "not configured" in h.detail
    c2 = _make(_server([]))
    assert (await c2.health()).ok
