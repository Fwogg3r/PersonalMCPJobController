"""Defang links in text a model wrote after reading untrusted content.

A rendered answer is itself a way out: a model told by a web page to "show this image" or "link to this URL" can put
private data in the query string, and chat apps unfurl (fetch) such links automatically. So for runs that read
untrusted content (`Caller.untrusted`), every http(s) or www. link whose host is not trusted is rewritten:

    https://evil.example/path?q=secret#x   ->   `hxxps://evil[.]example/path`

scheme http(s) becomes hxxp(s), the dots of the host become [.], the userinfo, query string and fragment are dropped, and
(in prose) the result sits in a code span, so no client turns it into a link, a preview or an image. Trusted hosts:

- config `output_link_allowlist` (domains, subdomains included; empty by default), and
- hosts of URLs the run's own tools returned or fetched before it read anything untrusted (results, not
  model-invented). Even then a query string or fragment is kept only if that exact URL appeared in a tool result.

Untainted runs are not touched. The function is idempotent (hxxps links aren't matched again).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import config

# http(s) URLs and bare www. hosts; stops at whitespace, brackets, quotes, backticks and backslashes
URL_RE = re.compile(r"(?i)(?<![\w@./-])(?:https?://|www\.)[^\s<>\[\]()`\"'\\]+")
_TRAIL = ".,;:!?*_~}"
MAX_SEEN = 400


def split_trailing(url: str) -> tuple[str, str]:
    """A URL as written in prose often ends in punctuation that isn't part of it."""
    core = url.rstrip(_TRAIL)
    return core, url[len(core):]


def urls_in(text: str) -> list[str]:
    return [split_trailing(m.group(0))[0] for m in URL_RE.finditer(text or "")]


def host_of(url: str) -> str:
    try:
        return (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return ""


def remember(seen: set[str], text: str) -> None:
    """Record the URLs of a tool result or fetched URL (bounded)."""
    for u in urls_in(text):
        if len(seen) >= MAX_SEEN:
            return
        seen.add(u)


def _allowed(host: str, domains: set[str]) -> bool:
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)


def _one(url: str, domains: set[str], seen: set[str], wrap: bool) -> str:
    host = host_of(url)
    if _allowed(host, domains):
        return url if url in seen or not re.search(r"[?#]", url) else re.split(r"[?#]", url, maxsplit=1)[0]
    try:
        p = urlsplit(url if "://" in url else "http://" + url)
        port = f":{p.port}" if p.port else ""
        path = p.path
    except ValueError:
        return "(link removed)"
    scheme = "hxxps" if url.lower().startswith("https") else "hxxp"
    out = f"{scheme}://{host.replace('.', '[.]')}{port}{path}"
    return f"`{out}`" if wrap else out


def text(value: str, tainted: bool, seen: set[str] | None = None, wrap: bool = True) -> str:
    """Defang `value` when `tainted`. `seen`: URLs the run's tools returned. wrap=False for text that is already inside
    a code block or JSON (a backtick would break it)."""
    if not tainted or not value:
        return value
    seen = seen or set()
    domains = {d.lower().lstrip(".") for d in config.load().output_link_allowlist if d}
    domains |= {h for h in (host_of(u) for u in seen) if h}

    def repl(m: re.Match) -> str:
        core, tail = split_trailing(m.group(0))
        return _one(core, domains, seen, wrap) + tail

    return URL_RE.sub(repl, value)


def for_caller(caller, value: str, wrap: bool = True) -> str:
    """Defang `value` if this caller's run has read untrusted content."""
    return text(value, bool(getattr(caller, "untrusted", False)), getattr(caller, "seen_urls", None), wrap)
