"""Finds connectors and exposes their tools to every interface (MCP, REST, CLI, dashboard, jobs).

Sources, in order (later ids override earlier ones):
1. built-in modules in pmjc.connectors
2. installed packages exposing the "pmjc.connectors" entry point (only if listed in settings.enabled_plugins)
3. Python files in PMJC_HOME/plugins/*.py
4. YAML files in PMJC_HOME/connectors.d/*.yaml: HTTP APIs, or `kind: command` for command-line tools
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import pkgutil
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import config
from .connector import Connector
import yaml

from .command_connector import load_command_connector
from .http_connector import load_yaml_connector

log = logging.getLogger("pmjc.registry")


@dataclass
class ToolSpec:
    name: str
    connector: str
    scope: str
    fn: Callable
    doc: str
    untrusted_output: bool
    egress: bool = False
    raw_doc: str = ""  # the docstring alone, without the connector's limits (used by compact schemas)
    schema: dict | None = None  # JSON schema passed through as-is (mounted MCP tools); else derived from the signature
    writes_state: bool = False  # writes durable trusted state (notes, contacts, jobs, watches): approval after taint
    private_output: bool = True  # result may hold the user's private data (False: public-web results)

    @property
    def mcp_scope(self) -> str:
        return {"act": "actions", "jobs": "jobs"}.get(self.scope, "sources")


def with_limits(doc: str, c: Connector) -> str:
    """Tool description plus the connector's limits, so a blank session sees them in the tool list."""
    extra = ""
    if c.limitations:
        extra += "\nLimits: " + c.limitations.strip()
    if c.native_alternatives:
        extra += "\nBetter if your client can: " + c.native_alternatives.strip()
    return doc + extra


def _classes_in(module) -> list[type[Connector]]:
    return [c for _, c in inspect.getmembers(module, inspect.isclass)
            if issubclass(c, Connector) and c.__module__ == module.__name__ and getattr(c, "id", "")]


class Registry:
    def __init__(self) -> None:
        self.connectors: dict[str, Connector] = {}
        self.errors: dict[str, str] = {}

    def load(self) -> "Registry":
        classes: list[type[Connector]] = []
        from . import connectors as builtin
        for m in pkgutil.iter_modules(builtin.__path__):
            try:
                classes += _classes_in(importlib.import_module(f"pmjc.connectors.{m.name}"))
            except Exception as e:  # a broken connector must not take the server down
                self.errors[m.name] = repr(e)
        enabled_eps = set(config.load().enabled_plugins)  # third-party code runs in-process: opt-in only
        for ep in importlib.metadata.entry_points(group="pmjc.connectors"):
            if ep.name not in enabled_eps:
                continue
            try:
                obj = ep.load()
                classes += [obj] if inspect.isclass(obj) else _classes_in(obj)
            except Exception as e:
                self.errors[ep.name] = repr(e)
        for f in sorted(config.path("plugins", "_").parent.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"pmjc_plugin_{f.stem}", f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                classes += _classes_in(mod)
            except Exception as e:
                self.errors[f.name] = repr(e)
        for f in sorted(config.path("connectors.d", "_").parent.glob("*.y*ml")):
            try:
                kind = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("kind", "http")
                classes.append(load_command_connector(f) if kind == "command" else load_yaml_connector(f))
            except Exception as e:
                self.errors[f.name] = repr(e)

        try:
            from .mcp_connector import mount_classes
            classes += mount_classes()  # existing MCP servers from settings.yaml
        except Exception as e:
            self.errors["mcp_servers"] = repr(e)

        disabled = set(config.load().disabled_connectors)
        for cls in classes:
            if cls.platforms and sys.platform not in cls.platforms:
                continue
            if cls.id in disabled:
                continue
            try:
                self.connectors[cls.id] = cls()
            except Exception as e:
                self.errors[cls.id] = repr(e)
        for k, v in self.errors.items():
            log.warning("connector %s failed to load: %s", k, v)
        return self

    def tools(self, include_unconfigured: bool = False) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        seen: set[str] = set()
        for c in self.connectors.values():
            if not include_unconfigured and not c.configured():
                continue
            for meta, fn in c.tools():
                if meta["name"] in seen:
                    log.warning("duplicate tool name %s in %s; skipped", meta["name"], c.id)
                    continue
                seen.add(meta["name"])
                out.append(ToolSpec(meta["name"], c.id, meta["scope"], fn, with_limits(inspect.getdoc(fn) or "", c),
                                    meta["untrusted_output"], meta.get("egress", False),
                                    raw_doc=inspect.getdoc(fn) or "",
                                    schema=getattr(fn, "__pmjc_schema__", None),
                                    writes_state=meta.get("writes_state", False),
                                    private_output=meta.get("private_output", True)))
        return out

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools(include_unconfigured=True) if t.name == name), None)

    def channels(self) -> list[Connector]:
        order = config.load().notify_channels
        chans = [c for c in self.connectors.values() if c.is_channel and c.configured()]
        return sorted(chans, key=lambda c: order.index(c.id) if c.id in order else 999)

    async def close(self) -> None:
        for c in self.connectors.values():
            await c.close()


_reg: Registry | None = None


def get() -> Registry:
    global _reg
    if _reg is None:
        _reg = Registry().load()
    return _reg


def reload() -> Registry:
    global _reg
    _reg = Registry().load()
    return _reg


def tool_json_schema(t: ToolSpec) -> dict[str, Any]:
    """JSON schema for a tool's parameters, derived from its signature (shared by REST and exports)."""
    if t.schema is not None:
        return json.loads(json.dumps(t.schema))
    from pydantic import TypeAdapter, create_model
    fields = {}
    for p in inspect.signature(t.fn).parameters.values():
        if p.name == "self" or p.name.startswith("_") or p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        ann = p.annotation if p.annotation is not inspect.Parameter.empty else Any
        fields[p.name] = (ann, ... if p.default is inspect.Parameter.empty else p.default)
    model = create_model(f"{t.name}_args", **fields)
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


# --- compact schemas for small-context providers (provider option schema_mode: compact) ---------------------
_SHORT_DEFAULT = 24  # keep a default only if its JSON is at most this many characters


def _compact_node(n: Any) -> Any:
    if isinstance(n, list):
        return [_compact_node(x) for x in n]
    if not isinstance(n, dict):
        return n
    out: dict[str, Any] = {}
    for k, v in n.items():
        if k == "title":
            continue
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {pk: _compact_node(pv) for pk, pv in v.items()}  # keys here are names, not keywords
        elif k == "default":
            if v is not None and len(json.dumps(v, default=str)) <= _SHORT_DEFAULT:
                out[k] = v
        else:
            out[k] = _compact_node(v)
    alts = out.get("anyOf")
    if isinstance(alts, list):
        rest = [a for a in alts if a != {"type": "null"}]
        if len(rest) != len(alts):
            del out["anyOf"]
            if len(rest) == 1 and isinstance(rest[0], dict):
                out = {**rest[0], **out}
            elif rest:
                out["anyOf"] = rest
    return out


def compact_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Same schema without titles, with `X or null` flattened to X, and only short defaults kept."""
    return _compact_node(schema)


def first_paragraph(doc: str) -> str:
    return (doc or "").strip().split("\n\n", 1)[0].strip()


def compact_tool_doc(t: ToolSpec) -> str:
    """First paragraph of the tool's docstring, without connector limits or client-only advice."""
    return first_paragraph(t.raw_doc or t.doc)


def compact_tool_schema(t: ToolSpec) -> dict[str, Any]:
    return compact_json_schema(tool_json_schema(t))
