"""Web search/fetch connector against a mocked transport; never touches the network or real DNS."""
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


def _opts(**kw):
    from pmjc import config
    s = config.load()
    s.connectors["web"] = kw
    config.save(s)


def _make(handler, monkeypatch, ips=None):
    from pmjc.connectors.web import Web
    c = Web()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def resolve(host):
        return (ips or {}).get(host, ["93.184.216.34"])
    monkeypatch.setattr(c, "_resolve", resolve)
    return c


PAGE = """<html><head><title> Hello  Page </title><style>.x{}</style><script>var secret=1;</script></head>
<body><nav>Home | About</nav><main><h1>Heading</h1><p>Real content here. %s</p>
<a href="/next">Next page</a> <a href="/next#top">dup</a> <a href="mailto:a@b.c">mail</a></main>
<footer>Copyright junk</footer></body></html>""" % ("word " * 60)


def test_extract_drops_boilerplate():
    from pmjc.connectors.web import extract
    ex = extract(PAGE)
    assert ex["title"] == "Hello Page"
    assert "Real content here" in ex["text"] and "Heading" in ex["text"]
    for junk in ("secret", "Home |", "Copyright", ".x{"):
        assert junk not in ex["text"]


def test_extract_without_main_uses_body():
    from pmjc.connectors.web import extract
    ex = extract("<html><body><script>x</script><div>Plain <b>text</b></div></body></html>")
    assert ex["text"] == "Plain text"


async def test_searxng_mapping_and_query_cap(monkeypatch):
    _opts(backend="searxng", searxng_url="http://searx.test:8080/")
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"results": [
            {"title": "A  title", "url": "https://a.test/", "content": "snip " * 100, "engine": "duckduckgo"},
            {"title": "B", "url": "https://b.test/", "content": "b", "engines": ["x", "y"]}]})
    c = _make(h, monkeypatch)
    out = await c.web_search([f"q{i}" for i in range(7)], max_results=1)
    assert len(seen) == 5 and seen[0].url.path == "/search" and seen[0].url.params["format"] == "json"
    assert str(seen[0].url).startswith("http://searx.test:8080/search")
    assert out[0]["rows"] == [{"title": "A title", "url": "https://a.test/", "snippet": ("snip " * 100).strip()[:300],
                               "source": "duckduckgo"}]
    assert "dropped" in out[-1]["note"]
    out = await c.web_search(["q"], max_results=5)
    assert out[0]["rows"][1]["source"] == "x,y"


async def test_brave_mapping_uses_stored_key(monkeypatch):
    from pmjc import creds
    _opts(backend="brave")
    creds.store().set("web.api_key", "BK")
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"web": {"results": [{"title": "T", "url": "https://t.test/", "description": "D"}]}})
    c = _make(h, monkeypatch)
    out = await c.web_search(["hello"], max_results=3)
    assert seen[0].headers["X-Subscription-Token"] == "BK"
    assert seen[0].url.params["q"] == "hello" and seen[0].url.params["count"] == "3"
    assert out[0]["rows"] == [{"title": "T", "url": "https://t.test/", "snippet": "D", "source": "brave"}]
    assert "BK" not in str(out)


async def test_search_error_is_per_query(monkeypatch):
    _opts(backend="searxng", searxng_url="http://s.test")
    c = _make(lambda req: httpx.Response(429), monkeypatch)
    out = await c.web_search(["a"])
    assert out[0]["rows"] == [] and "429" in out[0]["error"]


async def test_unconfigured_health_and_search(monkeypatch):
    from pmjc.connector import ConnectorError
    c = _make(lambda req: httpx.Response(500), monkeypatch)
    h = await c.health()
    assert not h.ok and "connectors.web.backend" in h.detail and "searxng" in h.detail and "brave" in h.detail
    with pytest.raises(ConnectorError):
        await c.web_search(["x"])
    _opts(backend="brave")  # key missing
    assert not (await c.health()).ok


async def test_fetch_text_links_and_truncation(monkeypatch):
    def h(req):
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=PAGE.encode())
    c = _make(h, monkeypatch)
    out = await c.web_fetch(["https://page.test/a"], max_chars=200, links=True)
    r = out[0]
    assert r["title"] == "Hello Page" and len(r["text"]) == 200 and r["truncated"] and r["chars_total"] > 200
    assert r["links"] == [{"text": "Next page", "href": "https://page.test/next"}]
    assert "secret" not in r["text"]


async def test_fetch_size_cap_and_url_cap(monkeypatch):
    _opts(max_bytes=1000)
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 50000)
    c = _make(h, monkeypatch)
    out = await c.web_fetch([f"https://p{i}.test/" for i in range(7)], max_chars=30000)
    assert len(calls) == 5
    assert len(out[0]["text"]) == 1000 and out[0]["truncated"]
    assert "dropped" in out[-1]["note"]


async def test_fetch_rejects_binary_and_errors(monkeypatch):
    def h(req):
        if req.url.path == "/img":
            return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG")
        return httpx.Response(404)
    c = _make(h, monkeypatch)
    out = await c.web_fetch(["https://x.test/img", "https://x.test/missing"])
    assert "not a text page" in out[0]["error"] and "404" in out[1]["error"]


async def test_redirects_followed_and_rechecked(monkeypatch):
    def h(req):
        if req.url.host == "a.test":
            return httpx.Response(302, headers={"location": "https://b.test/final"})
        if req.url.host == "b.test":
            return httpx.Response(302, headers={"location": "http://evil.test/"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")
    c = _make(h, monkeypatch, ips={"evil.test": ["10.0.0.9"]})
    out = await c.web_fetch(["https://a.test/"])
    assert "private" in out[0]["error"]

    def h2(req):
        if req.url.host == "z.test":
            return httpx.Response(302, headers={"location": "https://a.test/"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok")
    c2 = _make(h2, monkeypatch)
    out = await c2.web_fetch(["https://z.test/"])
    assert out[0]["final_url"] == "https://a.test/" and out[0]["text"] == "ok"


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.1.2.3/x", "http://localhost:8080/", "http://[::1]/",
                                 "http://169.254.169.254/latest", "http://192.168.1.1/", "http://foo.localhost/",
                                 "http://[::ffff:127.0.0.1]/", "file:///etc/passwd", "ftp://x.test/"])
async def test_ssrf_guard_refuses(url, monkeypatch):
    def h(req):
        raise AssertionError("must not connect")
    c = _make(h, monkeypatch)
    out = await c.web_fetch([url])
    assert "error" in out[0] and "ConnectorError" in out[0]["error"]


async def test_ssrf_dns_resolving_to_private(monkeypatch):
    c = _make(lambda req: httpx.Response(200, content=b"x"), monkeypatch,
              ips={"sneaky.test": ["93.184.216.34", "192.168.0.5"]})
    out = await c.web_fetch(["https://sneaky.test/"])
    assert "private" in out[0]["error"]


async def test_allow_private_option(monkeypatch):
    _opts(allow_private=True)
    c = _make(lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"lan"), monkeypatch)
    out = await c.web_fetch(["http://127.0.0.1/"])
    assert out[0]["text"] == "lan"


async def test_blocked_domains(monkeypatch):
    _opts(blocked_domains=["bad.test"])
    c = _make(lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"ok"), monkeypatch)
    out = await c.web_fetch(["https://bad.test/", "https://sub.bad.test/", "https://notbad.test/"])
    assert "blocked" in out[0]["error"] and "blocked" in out[1]["error"] and out[2]["text"] == "ok"


def test_tool_flags():
    from pmjc import registry
    reg = registry.reload()
    for n in ("web_search", "web_fetch"):
        t = reg.tool(n)
        assert t.scope == "read" and t.untrusted_output and t.egress, n


def _tainted(**kw):
    from pmjc import auth
    c = auth.Caller("job:x", "job", {"sources"}, **kw)
    c.untrusted = True
    return c


def test_declared_read_for_web_fetch():
    from pmjc import executor
    c = _tainted(read_urls=("https://a.test/x",), read_sites=("example.com",))

    def ok(urls):
        return executor.declared_read(c, "web_fetch", {"urls": urls})
    assert ok(["https://a.test/x"])
    assert ok(["https://a.test/x", "https://docs.example.com/p"])
    assert not ok(["https://a.test/x", "https://other.test/"])  # one undeclared
    assert not ok(["https://a.test/x?q=secret"])
    assert not ok([]) and not ok(None)
    assert not executor.declared_read(_tainted(), "web_fetch", {"urls": ["https://a.test/x"]})
    assert not executor.declared_read(c, "web_search", {"queries": ["https://a.test/x"]})
