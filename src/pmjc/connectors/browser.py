"""A signed-in browser for services without an API (Facebook/Messenger, Marketplace, ...).

pmjc keeps its own Chromium profile in PMJC_HOME/browser (separate from your everyday Chrome). Sign in once:
    pmjc browser login https://www.messenger.com
Agents can then read pages on allowed domains only (option `allowed_domains`).
Needs: uv sync --extra browser && uv run playwright install chromium
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlparse

from .. import config
from ..connector import Connector, ConnectorError, Health, tool

DEFAULT_DOMAINS = ["facebook.com", "messenger.com", "discord.com"]


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

    async def _context(self, headless: bool = True):
        from playwright.async_api import async_playwright
        if self._ctx is None:
            self._pw = await async_playwright().start()
            args = []
            if not headless:
                x, y = rightmost_monitor_origin()
                args.append(f"--window-position={x + 40},{y + 40}")
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(config.path("browser", "profile", "_").parent), headless=headless, args=args,
                viewport={"width": 1280, "height": 900})
        return self._ctx

    async def close(self) -> None:
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()
        self._ctx = self._pw = None
        await super().close()

    @tool(scope="read", untrusted_output=True, egress=True)
    async def browser_read(self, url: str, wait_seconds: float = 3.0, max_chars: int = 15000) -> dict:
        """Open a page (on an allowed domain) in the signed-in browser and return its visible text."""
        self._check(url)
        async with self._lock:
            ctx = await self._context()
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(min(max(wait_seconds, 0), 15))
                self._check(page.url)  # redirects must stay on allowed domains too
                text = await page.inner_text("body", timeout=15000)
                return {"url": page.url, "title": await page.title(), "text": text[:max_chars]}
            finally:
                await page.close()

    async def login(self, url: str) -> None:
        """Opens a visible window (rightmost monitor) so the user can sign in; closes when they close it."""
        ctx = await self._context(headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        while ctx.pages:
            await asyncio.sleep(1)
        await self.close()

    async def health(self) -> Health:
        return Health(self.configured(), "ready" if self.configured() else "install with: uv sync --extra browser")
