"""The connector contract. A connector is one class in one file.

    class Weather(Connector):
        id = "weather"
        name = "Weather (Open-Meteo)"
        description = "Forecasts for any place. No account needed."
        credentials = []                      # Field(...) list; secrets go to the credential store

        @tool(scope="read")
        async def weather_forecast(self, lat: float, lon: float, days: int = 3) -> dict:
            "Daily forecast for a location."
            ...

        @event_source(interval=300)           # optional: return/yield Event objects
        async def poll(self): ...

        async def notify(self, title, message, actions=None): ...   # optional: makes it an output channel

Tool names must be unique, use letters/digits/underscores, and should start with the connector id.
"""
from __future__ import annotations

import hashlib
import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import httpx

from . import config, creds, db

Scope = Literal["read", "act", "jobs"]  # jobs = pmjc job management (needs the jobs scope)


@dataclass
class Field:
    """A setting the user provides for a connector."""
    name: str
    label: str = ""
    secret: bool = True
    required: bool = True
    help: str = ""
    kind: str = "text"  # text | oauth2 | browser_login
    default: str | None = None


@dataclass
class Event:
    source: str
    kind: str
    title: str = ""
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    key: str = ""  # dedupe key; derived from content if empty
    untrusted: bool = True  # third-party content by default

    def __post_init__(self) -> None:
        if not self.key:
            self.key = hashlib.sha256(f"{self.source}|{self.kind}|{self.title}|{self.text}|{int(self.ts)}".encode()).hexdigest()[:32]


@dataclass
class Health:
    ok: bool
    detail: str = ""


TOOL_NAME = re.compile(r"^[a-zA-Z0-9_]{1,64}$")


def tool(fn: Callable | None = None, *, scope: Scope = "act", name: str | None = None,
         untrusted_output: bool = False, egress: bool = False, writes_state: bool = False,
         private_output: bool = True):
    """Mark a connector method as a tool. Fail-closed defaults:

    scope="act" (default) means the call changes something outside pmjc (sends, posts, buys, switches);
      every act call needs approval unless it is on the user's auto-approve list. Say scope="read" explicitly.
    untrusted_output=True: returns third-party content (messages, web pages, notifications). Once a run calls
      such a tool it is tainted: no act tools without approval, no egress tools.
    egress=True: model-controlled arguments leave the machine (search queries, URLs to fetch), so it could leak
      data; tainted runs can't call it.
    writes_state=True: changes durable state that later runs trust (notes, contacts, jobs, watches). A run that has
      read untrusted content gets an approval request instead of the call, so injected text can't be laundered into
      memory. Opt in on every new tool of that kind; mounted MCP tools are `act` and need approval anyway.
    private_output=False: the result is public-web data (search, fetch, weather), not the user's private data.
      The default is True (fail-closed): a run that calls such a tool is marked `sensitive` (Caller.sensitive).
    """
    def wrap(f: Callable) -> Callable:
        n = name or f.__name__
        if not TOOL_NAME.match(n):
            raise ValueError(f"tool name {n!r} must match {TOOL_NAME.pattern}")
        f.__pmjc_tool__ = {"scope": scope, "name": n, "untrusted_output": untrusted_output, "egress": egress,
                           "writes_state": writes_state, "private_output": private_output}
        return f
    return wrap(fn) if fn else wrap


def event_source(fn: Callable | None = None, *, interval: int = 60):
    """Mark a method that produces events. Called every `interval` seconds; returns or yields Event objects."""
    def wrap(f: Callable) -> Callable:
        f.__pmjc_events__ = {"interval": interval}
        return f
    return wrap(fn) if fn else wrap


class ConnectorError(Exception):
    pass


class NotConfigured(ConnectorError):
    pass


class Connector:
    id: str = ""
    name: str = ""
    description: str = ""
    credentials: list[Field] = []
    platforms: list[str] | None = None  # e.g. ["win32"]; None = all
    docs_url: str = ""
    # What this connector can't do, and what a client with better abilities should use instead. The registry appends
    # both to every tool description so a model with no other context sees them in the tool list.
    limitations: str = ""
    native_alternatives: str = ""

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None

    @property
    def type_id(self) -> str:
        """The connector type ("google"); `id` is "google:work" for a non-default instance."""
        return self.id.partition(":")[0]

    @property
    def instance(self) -> str:
        """Instance name ("work"), empty for the default instance."""
        return self.id.partition(":")[2]

    # --- settings and secrets ---------------------------------------------------
    def secret(self, name: str, required: bool = True) -> str | None:
        v = creds.store().get(f"{self.id}.{name}")
        if v is None:
            f = next((f for f in self.credentials if f.name == name), None)
            v = self.option(name, f.default if f else None)
        if required and not v:
            raise NotConfigured(f"{self.id}: '{name}' is not set. Run: pmjc auth set {self.id}  (or use the dashboard)")
        return v

    def option(self, name: str, default: Any = None) -> Any:
        return config.load().connectors.get(self.id, {}).get(name, default)

    def configured(self) -> bool:
        for f in self.credentials:
            if not f.required or f.kind != "text":
                continue
            key = f"{self.id}.{f.name}"
            if not (creds.store().has(key) or self.option(f.name) or f.default):
                return False
        return True

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "pmjc/0.1"})
        return self._http

    # --- small persistent state (cursors, last-seen ids) ------------------------------
    def state_get(self, name: str, default: Any = None) -> Any:
        return db.kv_get(f"state:{self.id}:{name}", default)

    def state_set(self, name: str, value: Any) -> None:
        db.kv_set(f"state:{self.id}:{name}", value)

    # --- optional hooks -----------------------------------------------------------
    async def health(self) -> Health:
        return Health(self.configured(), "configured" if self.configured() else "needs credentials")

    def on_poll_start(self) -> None:
        """Called when background polling of this connector (re)starts; reset 'already seen' state here."""

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # --- introspection ------------------------------------------------------------
    def tools(self) -> list[tuple[dict, Callable]]:
        out = []
        for _, m in inspect.getmembers(self, predicate=inspect.ismethod):
            meta = getattr(m, "__pmjc_tool__", None)
            if meta:
                out.append((meta, m))
        return out

    def event_sources(self) -> list[tuple[dict, Callable]]:
        out = []
        for _, m in inspect.getmembers(self, predicate=inspect.ismethod):
            meta = getattr(m, "__pmjc_events__", None)
            if meta:
                out.append((meta, m))
        return out

    @property
    def is_channel(self) -> bool:
        return type(self).notify is not Connector.notify

    async def notify(self, title: str, message: str, actions: list[dict] | None = None) -> None:
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "id": self.id, "type": self.type_id, "instance": self.instance,
            "name": f"{self.name} [{self.instance}]" if self.instance else self.name, "description": self.description, "docs_url": self.docs_url,
            "limitations": self.limitations, "native_alternatives": self.native_alternatives,
            "credentials": [f.__dict__ for f in self.credentials],
            "configured": self.configured(), "channel": self.is_channel,
            "tools": [{"name": m["name"], "scope": m["scope"], "egress": m["egress"], "untrusted_output": m["untrusted_output"],
                       "writes_state": m.get("writes_state", False), "private_output": m.get("private_output", True),
                       "doc": inspect.getdoc(fn) or ""} for m, fn in self.tools()],
            "event_sources": [m for m, _ in self.event_sources()],
        }
