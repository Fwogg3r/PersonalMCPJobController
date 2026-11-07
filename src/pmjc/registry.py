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
                                    meta["untrusted_output"], meta.get("egress", False)))
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
