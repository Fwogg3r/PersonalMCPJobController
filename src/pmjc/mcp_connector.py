"""Mount existing MCP servers as pmjc connectors (settings.yaml `mcp_servers:`).

    mcp_servers:
      - id: files
        transport: stdio                      # stdio | http (streamable HTTP)
        command: npx                          # argv, never a shell; only editable in settings.yaml
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/some/folder"]
        env: [SOME_TOKEN]                     # names pulled from the credential store as mcp.files.SOME_TOKEN
        enabled: true
        tools: [read_text_file]               # optional allowlist (server's own tool names)
        exclude: [write_file]
        overrides:
          read_text_file: {scope: read, untrusted_output: true, egress: false}

      - id: remote
        transport: http
        url: https://example.com/mcp
        bearer_credential: mcp.remote.bearer  # credential-store key (default), never inline

Every mounted tool is named `<id>__<tool>` and goes through pmjc's executor like any other tool. Fail-closed defaults:
scope `act` (needs approval) unless the server marks the tool `readOnlyHint: true`; `untrusted_output` and `egress`
are true. Only the per-tool `overrides` in settings.yaml can relax that.

A dedicated thread runs one event loop for all mounted servers, so a connection outlives any single request and
the sync registry can discover tools at load time. Connections are re-opened on failure.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import Future
from typing import Any

from . import config, creds
from .connector import Connector, ConnectorError, Health

log = logging.getLogger("pmjc.mcp_connector")

SEP = "__"
_BAD = re.compile(r"[^a-zA-Z0-9_]")


def sanitize_name(server_id: str, tool: str) -> str:
    """`<id>__<tool>` in the charset pmjc allows (letters, digits, underscore), at most 64 characters."""
    name = f"{_BAD.sub('_', server_id)}{SEP}{_BAD.sub('_', tool)}"
    if len(name) > 64:
        name = name[:55] + "_" + hashlib.sha256(f"{server_id}/{tool}".encode()).hexdigest()[:8]
    return name


class _Loop:
    """One background event loop shared by every mounted server."""
    _lock = threading.Lock()
    _loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def get(cls) -> asyncio.AbstractEventLoop:
        with cls._lock:
            if cls._loop is None or cls._loop.is_closed():
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="pmjc-mcp-client", daemon=True).start()
                cls._loop = loop
            return cls._loop

    @classmethod
    def submit(cls, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, cls.get())


class Session:
    """One live connection to a mounted server (lives on the shared loop)."""

    def __init__(self, entry: "config.McpServerConfig", env: dict[str, str], bearer: str | None) -> None:
        self.entry, self._env, self._bearer = entry, env, bearer
        self.tools: list[Any] = []
        self.error: str = ""
        self._client: Any = None
        self._lock: asyncio.Lock | None = None
        self._pending: Future | None = None

    def _make_client(self):
        from fastmcp import Client
        e = self.entry
        if e.transport == "http":
            from fastmcp.client.transports import StreamableHttpTransport
            headers = {"Authorization": f"Bearer {self._bearer}"} if self._bearer else {}
            return Client(StreamableHttpTransport(e.url, headers=headers), timeout=e.call_timeout)
        from fastmcp.client.transports import StdioTransport
        return Client(StdioTransport(command=e.command, args=list(e.args), env=dict(self._env) or None,
                                     keep_alive=True), timeout=e.call_timeout)

    async def _connect(self) -> None:
        await self._drop()
        client = self._make_client()
        try:
            await asyncio.wait_for(client.__aenter__(), self.entry.timeout)
            tools = await asyncio.wait_for(client.list_tools(), self.entry.timeout)
        except BaseException as ex:
            try:
                await client.__aexit__(None, None, None)
            except BaseException:
                pass
            self.error = (f"{type(ex).__name__}: {ex}" if str(ex) else type(ex).__name__)[:300]
            raise ConnectorError(f"mcp server {self.entry.id} unavailable: {self.error}") from None
        self._client, self.tools, self.error = client, tools, ""

    async def _drop(self) -> None:
        c, self._client = self._client, None
        if c is not None:
            try:
                await c.__aexit__(None, None, None)
            except BaseException:
                pass

    async def ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._client is None or not self._client.is_connected():
                await self._connect()

    def start(self) -> None:
        """Begin connecting without waiting (so several servers start in parallel)."""
        if self._pending is None:
            self._pending = _Loop.submit(self.ensure())

    def wait(self, timeout: float) -> None:
        self.start()
        try:
            self._pending.result(timeout * 2 + 2)  # type: ignore[union-attr]
        except BaseException as e:
            if not self.error:
                self.error = type(e).__name__
            log.warning("mcp server %s not available: %s", self.entry.id, self.error)

    async def reconnect_if_needed(self) -> bool:
        try:
            await self.ensure()
            return True
        except ConnectorError:
            return False

    async def call(self, tool: str, args: dict) -> Any:
        for attempt in (0, 1):
            await self.ensure()
            try:
                return await self._client.call_tool(tool, args, raise_on_error=False)
            except Exception as e:  # transport failure (tool errors come back as results): reconnect once
                self.error = type(e).__name__
                await self._drop()
                if attempt:
                    raise ConnectorError(f"mcp server {self.entry.id} failed: {type(e).__name__}") from None

    def close(self) -> None:
        try:
            _Loop.submit(self._drop()).result(10)
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._client is not None


_pool: dict[str, tuple[str, Session]] = {}


def _fingerprint(entry: "config.McpServerConfig", env: dict, bearer: str | None) -> str:
    return hashlib.sha256(json.dumps([entry.model_dump(exclude={"enabled", "tools", "exclude", "overrides"}),
                                      env, bearer], sort_keys=True, default=str).encode()).hexdigest()


def session_for(entry: "config.McpServerConfig") -> Session:
    store = creds.store()
    env = {n: v for n in entry.env if (v := store.get(f"mcp.{entry.id}.{n}"))}
    bearer = store.get(entry.bearer_credential or f"mcp.{entry.id}.bearer") if entry.transport == "http" else None
    fp = _fingerprint(entry, env, bearer)
    old = _pool.get(entry.id)
    if old and old[0] == fp:
        return old[1]
    if old:
        old[1].close()
    s = Session(entry, env, bearer)
    _pool[entry.id] = (fp, s)
    return s


async def _call(session: Session, tool: str, args: dict, limit: int) -> Any:
    return _text(await asyncio.wrap_future(_Loop.submit(session.call(tool, args))), limit)


def _text(res: Any, limit: int) -> Any:
    """CallToolResult -> JSON-able. Text and structured output are kept; images and other blobs are noted as omitted."""
    parts: list[str] = []
    for b in getattr(res, "content", None) or []:
        t = getattr(b, "type", "")
        if t == "text":
            parts.append(b.text)
        elif t == "image":
            parts.append("[image omitted]")
        elif t == "audio":
            parts.append("[audio omitted]")
        elif t == "resource":
            r = b.resource
            parts.append(r.text if getattr(r, "text", None) is not None else f"[binary resource omitted: {getattr(r, 'uri', '')}]")
        else:
            parts.append(f"[{t or 'content'} omitted]")
    text = "\n".join(parts)
    if getattr(res, "is_error", False):
        raise ConnectorError(text[:2000] or "the mounted tool reported an error")
    sc = getattr(res, "structured_content", None)
    out: Any = sc if sc is not None and not text.strip() else text
    if isinstance(out, str) and len(out) > limit:
        out = out[:limit] + f"\n[truncated at {limit} characters]"
    elif not isinstance(out, str) and len(json.dumps(out, default=str)) > limit:
        out = json.dumps(out, default=str)[:limit] + f"\n[truncated at {limit} characters]"
    return out


def effective_flags(entry: "config.McpServerConfig", tool: Any) -> dict:
    ann = getattr(tool, "annotations", None)  # field is read_only_hint in newer SDKs, readOnlyHint in older ones
    read_only = (getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", None)) is True
    o = entry.overrides.get(tool.name)
    return {
        "scope": o.scope if o and o.scope else ("read" if read_only else "act"),
        "untrusted_output": o.untrusted_output if o and o.untrusted_output is not None else True,
        "egress": o.egress if o and o.egress is not None else True,
    }


class McpConnector(Connector):
    """Base class; `mount_classes` makes one subclass per configured server."""
    entry: "config.McpServerConfig"
    session: Session

    def __init__(self) -> None:
        super().__init__()
        self.session = session_for(self.entry)
        self.session.wait(self.entry.timeout)

    def _selected(self) -> list[Any]:
        e = self.entry
        return [t for t in self.session.tools
                if (e.tools is None or t.name in e.tools) and t.name not in e.exclude]

    def tools(self) -> list[tuple[dict, Any]]:
        out, seen = [], set()
        for t in self._selected():
            name = sanitize_name(self.entry.id, t.name)
            while name in seen:
                name = name[:60] + "_" + hashlib.sha256(t.name.encode()).hexdigest()[:3]
            seen.add(name)
            schema = dict(getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})

            async def fn(_pmjc_s=self.session, _pmjc_o=t.name, _pmjc_l=self.entry.max_output, **kwargs):
                return await _call(_pmjc_s, _pmjc_o, kwargs, _pmjc_l)

            fn.__name__ = name
            fn.__doc__ = (t.description or t.title or t.name).strip()
            fn.__pmjc_schema__ = schema  # type: ignore[attr-defined]
            out.append(({"name": name, **effective_flags(self.entry, t)}, fn))
        return out

    async def health(self) -> Health:
        ok = self.session.connected
        if not ok:
            try:
                ok = await asyncio.wait_for(asyncio.wrap_future(_Loop.submit(self.session.reconnect_if_needed())),
                                            self.entry.timeout * 2 + 5)
            except Exception:
                ok = False
        if ok:
            return Health(True, f"connected, {len(self._selected())} tools")
        return Health(False, f"failed: {self.session.error or 'not connected'}")

    def describe(self) -> dict:
        d = super().describe()
        d["mcp"] = {"transport": self.entry.transport, "connected": self.session.connected,
                    "error": self.session.error, "tools_available": len(self.session.tools)}
        return d


def shutdown() -> None:
    for _, s in list(_pool.values()):
        s.close()
    _pool.clear()


def mount_classes() -> list[type[Connector]]:
    """One connector class per enabled server in settings.yaml. Connections start in parallel here."""
    classes: list[type[Connector]] = []
    for e in config.load().mcp_servers:
        if not e.enabled:
            continue
        session_for(e).start()
        classes.append(type(f"Mcp_{e.id}", (McpConnector,), {
            "id": e.id, "name": f"MCP server {e.id}", "entry": e,
            "description": f"Tools mounted from the MCP server '{e.id}' ({e.transport}).",
            "credentials": []}))
    return classes
