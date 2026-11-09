"""A signed-in browser for services without an API (Facebook/Messenger, Marketplace, ...).

pmjc keeps its own Chromium profile in PMJC_HOME/browser (separate from your everyday Chrome). Sign in once:
    pmjc browser login https://www.messenger.com
Agents can then read pages on allowed domains only (option `allowed_domains`).
Options (connectors.browser in settings): allowed_domains, channel ("" = bundled Chromium, "chrome" = installed
Chrome), headless (default true; when false the window opens on the rightmost monitor without taking focus).
Needs: uv sync --extra browser && uv run playwright install chromium
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import urldefrag, urljoin, urlparse

from .. import config
from ..connector import Connector, ConnectorError, Health, tool

DEFAULT_DOMAINS = ["facebook.com", "messenger.com", "discord.com"]

_LINKS_JS = ("els => els.map(a => ({text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim(),"
             " href: a.getAttribute('href')}))")
_ROWS_JS = """(rows, fields) => rows.map(row => {
  const out = {};
  for (const [name, spec] of Object.entries(fields)) {
    const sel = spec[0], attr = spec[1];
    const el = sel ? row.querySelector(sel) : row;
    if (!el) { out[name] = null; continue; }
    out[name] = attr ? el.getAttribute(attr) : (el.innerText || el.textContent || '').trim();
  }
  return out;
})"""
_URL_ATTRS = {"href", "src", "action", "data-href"}


def clean_links(raw: list[dict], base: str, limit: int) -> list[dict]:
    """Absolute http(s) links, deduplicated by href, at most `limit`."""
    out, seen = [], set()
    for r in raw:
        if len(out) >= max(limit, 0):
            break
        href = (r.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urldefrag(urljoin(base, href))[0]
        if urlparse(absolute).scheme not in ("http", "https") or absolute in seen:
            continue
        seen.add(absolute)
        out.append({"text": " ".join((r.get("text") or "").split())[:200], "href": absolute})
    return out


def parse_fields(fields: dict) -> dict[str, tuple[str, str | None]]:
    """{"link": "a@href", "title": "h3", "id": "@data-id"} -> {name: (sub_selector, attribute or None)}."""
    if not isinstance(fields, dict) or not fields:
        raise ConnectorError('fields must be a non-empty object like {"title": "h3", "link": "a@href"}')
    out = {}
    for name, spec in fields.items():
        spec = str(spec)
        sel, _, attr = spec.rpartition("@") if "@" in spec else (spec, "", "")
        out[str(name)] = (sel.strip(), attr.strip() or None)
    return out


def finish_rows(rows: list[dict], parsed: dict, base: str) -> list[dict]:
    """Tidy whitespace, make URL attributes absolute, cap value length."""
    for row in rows:
        for name, (_sel, attr) in parsed.items():
            v = row.get(name)
            if isinstance(v, str):
                v = " ".join(v.split()) if attr is None else v.strip()
                if attr in _URL_ATTRS and v:
                    v = urljoin(base, v)
                row[name] = v[:1000]
    return rows


def rightmost_monitor_origin() -> tuple[int, int]:
    """Top-left of the rightmost monitor, so login windows never land on the primary screen when others exist."""
    if sys.platform != "win32":
        return (0, 0)
    import ctypes
    from ctypes import wintypes
    rects = []
    cb = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def f(_h, _dc, r, _l):
        rects.append((r.contents.left, r.contents.top))
        return 1
    ctypes.windll.user32.EnumDisplayMonitors(0, 0, cb(f), 0)
    return max(rects) if rects else (0, 0)


class Browser(Connector):
    id = "browser"
    limitations = ("Read-only: can't click, type, submit or download. Returns page text and links, or structured rows "
                   "(browser_extract). Only domains allowed in settings; needs a saved login (pmjc browser login <url>). "
                   "Sends the URL off the machine.")
    native_alternatives = 'browser or computer control in your client, to interact with pages.'
    name = "Browser (signed-in profile)"
    description = "Reads pages from sites you've signed in to in pmjc's own browser profile. Run `pmjc browser login <url>` once per site."
    credentials = []
    docs_url = "https://playwright.dev/python/"

    def __init__(self) -> None:
        super().__init__()
        self._pw = None
        self._ctx = None
        self._lock = asyncio.Lock()

    def configured(self) -> bool:
        try:
            import playwright  # noqa: F401
            return True
        except ImportError:
            return False

    def _domains(self) -> list[str]:
        return self.option("allowed_domains", DEFAULT_DOMAINS)

    def _check(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        if urlparse(url).scheme not in ("http", "https") or not any(host == d or host.endswith("." + d) for d in self._domains()):
            raise ConnectorError(f"{host or url} isn't in browser.allowed_domains {self._domains()}")

    async def _context(self, headless: bool | None = None, focus: bool = True):
        """headless=None follows connectors.browser.headless (default true). A headed window opens on the rightmost
        monitor; unless `focus` (the login flow) it must not take focus."""
        from playwright.async_api import async_playwright
        if headless is None:
            headless = bool(self.option("headless", True))
        if self._ctx is None:
            self._pw = await async_playwright().start()
            args = []
            if not headless:
                x, y = rightmost_monitor_origin()
                args.append(f"--window-position={x + 40},{y + 40}")
                if not focus:
                    args.append("--no-startup-window-focus")
            kw = {}
            channel = str(self.option("channel", "") or "").strip()
            if channel:
                kw["channel"] = channel  # e.g. "chrome" = the installed Chrome instead of bundled Chromium
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(config.path("browser", "profile", "_").parent), headless=headless, args=args,
                viewport={"width": 1280, "height": 900}, **kw)
        return self._ctx

    async def _open(self, url: str, wait_seconds: float):
        ctx = await self._context(focus=False)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(min(max(wait_seconds, 0), 15))
            self._check(page.url)  # redirects must stay on allowed domains too
        except BaseException:
            await page.close()
            raise
        return page

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()
        self._ctx = self._pw = None
        await super().close()

    @tool(scope="read", untrusted_output=True, egress=True)
    async def browser_read(self, url: str, wait_seconds: float = 3.0, max_chars: int = 15000, max_links: int = 40) -> dict:
        """Open a page in pmjc's signed-in browser and return its visible text plus `links` (text and absolute href, deduplicated, at most max_links; 0 = none). url must be on a domain the user allowed (the error lists them). Read-only: it can't click or type."""
        self._check(url)
        async with self._lock:
            page = await self._open(url, wait_seconds)
            try:
                text = await page.inner_text("body", timeout=15000)
                links = []
                if max_links > 0:
                    raw = await page.eval_on_selector_all("a[href]", _LINKS_JS)
                    links = clean_links(raw, page.url, min(max_links, 200))
                return {"url": page.url, "title": await page.title(), "text": text[:max_chars], "links": links}
            finally:
                await page.close()

    @tool(scope="read", untrusted_output=True, egress=True)
    async def browser_extract(self, url: str, selector: str, fields: dict, max_items: int = 30, wait_seconds: float = 3.0) -> dict:
        """Open a page and return structured rows. `selector` is a CSS selector for each repeated row (e.g. "li.result"); `fields` maps a name to a sub-selector inside the row, optionally with @attribute: {"title": "h3", "price": ".price", "link": "a@href"} (a bare "@href" reads the row's own attribute). Text fields are visible text; href/src values are made absolute. Same domain rules as browser_read."""
        self._check(url)
        parsed = parse_fields(fields)
        async with self._lock:
            page = await self._open(url, wait_seconds)
            try:
                n = min(max(int(max_items), 1), 200)
                rows = await page.eval_on_selector_all(selector, _ROWS_JS, {k: list(v) for k, v in parsed.items()})
                rows = finish_rows(rows[:n], parsed, page.url)
                return {"url": page.url, "title": await page.title(), "count": len(rows), "rows": rows}
            finally:
                await page.close()

    async def login(self, url: str) -> None:
        """Opens a visible window (rightmost monitor) so the user can sign in; closes when they close it."""
        ctx = await self._context(headless=False, focus=True)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        while ctx.pages:
            await asyncio.sleep(1)
        await self.close()

    async def health(self) -> Health:
        return Health(self.configured(), "ready" if self.configured() else "install with: uv sync --extra browser")
