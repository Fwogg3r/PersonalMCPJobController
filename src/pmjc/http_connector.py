"""Declarative connectors: any plain HTTP API as a YAML file in PMJC_HOME/connectors.d/.

    id: jokes
    name: JokeAPI
    description: Random jokes.
    base_url: https://v2.jokeapi.dev
    auth: {type: none}                 # none | bearer | header | query | basic
    credentials: []                    # e.g. [{name: api_key, label: API key}]
    tools:
      - name: jokes_random
        description: A random joke.
        method: GET
        path: /joke/{category}
        scope: read
        params:
          category: {type: string, in: path, default: Any, description: Programming, Pun, ...}
          safe: {type: boolean, in: query, default: true}

Fail-closed defaults: scope act, untrusted_output true, egress true when the tool has parameters.
Credentials are only ever sent to base_url (path parameters are URL-quoted, so they can't change the host).

Auth: bearer -> "Authorization: Bearer <api_key>"; header -> "<header>: <api_key>";
query -> "?<param>=<api_key>"; basic -> username/password fields. The secret field defaults to "api_key"
(override with auth.field).
"""
from __future__ import annotations

import inspect
import keyword
import re
from urllib.parse import quote
from pathlib import Path
from typing import Any

import yaml

from .connector import Connector, Field, tool

_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict, "array": list}


def _pyname(name: str) -> str:
    n = re.sub(r"\W", "_", name)
    return n if n.isidentifier() and not keyword.iskeyword(n) else f"p_{n}"


def _make_tool(spec: dict) -> Any:
    params: dict = spec.get("params") or {}
    sig_params = []
    for wire, p in params.items():
        pname = _pyname(wire)
        ann = _TYPES.get(p.get("type", "string"), str)
        required = p.get("required", "default" not in p)
        default = inspect.Parameter.empty if required else p.get("default")
        if not required and default is None:
            ann = ann | None
        sig_params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
    sig_params.sort(key=lambda x: x.default is not inspect.Parameter.empty)

    async def fn(self, **kwargs):
        return await self._call(spec, {wire: kwargs.get(_pyname(wire)) for wire in params})

    fn.__name__ = spec["name"]
    fn.__doc__ = spec.get("description", "") + "".join(
        f"\n{n}: {p.get('description', '')}" for n, p in params.items() if p.get("description"))
    fn.__signature__ = inspect.Signature([inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD), *sig_params],
                                         return_annotation=Any)
    fn.__annotations__ = {p.name: p.annotation for p in sig_params} | {"return": Any}
    return tool(fn, scope=spec.get("scope", "act"), untrusted_output=spec.get("untrusted_output", True),
                egress=spec.get("egress", bool(params)), level=spec.get("level"))


class HttpConnector(Connector):
    spec: dict = {}

    async def _call(self, spec: dict, args: dict) -> Any:
        params: dict = spec.get("params") or {}
        path = spec["path"]
        query, body, headers = {}, {}, dict(self.spec.get("headers") or {})
        for name, p in params.items():
            if name not in args or args[name] is None:
                if "default" in p and p["default"] is not None:
                    args[name] = p["default"]
                else:
                    continue
            where = p.get("in", "query")
            if where == "path":
                path = path.replace("{" + name + "}", quote(str(args[name]), safe=""))  # can't change the host
            elif where == "body":
                body[name] = args[name]
            elif where == "header":
                headers[p.get("header", name)] = str(args[name])
            else:
                query[name] = args[name]
        auth = self.spec.get("auth") or {"type": "none"}
        t = auth.get("type", "none")
        httpx_auth = None
        if t != "none":
            if t == "basic":
                httpx_auth = (self.secret("username"), self.secret("password"))
            else:
                key = self.secret(auth.get("field", "api_key"))
                if t == "bearer":
                    headers["Authorization"] = f"Bearer {key}"
                elif t == "header":
                    headers[auth["header"]] = key
                elif t == "query":
                    query[auth["param"]] = key
        r = await self.http.request(spec.get("method", "GET"), self.spec["base_url"].rstrip("/") + path,
                                    params=query or None, json=body or None, headers=headers, auth=httpx_auth)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        return r.json() if "json" in ct else {"status": r.status_code, "text": r.text[:20000]}


def load_yaml_connector(file: Path) -> type[Connector]:
    spec = yaml.safe_load(file.read_text(encoding="utf-8"))
    attrs: dict[str, Any] = {
        "id": spec["id"], "name": spec.get("name", spec["id"]), "description": spec.get("description", ""),
        "docs_url": spec.get("docs_url", ""), "spec": spec,
        "limitations": spec.get("limitations", ""), "native_alternatives": spec.get("native_alternatives", ""),
        "credentials": [Field(**c) for c in spec.get("credentials") or []],
    }
    for t in spec.get("tools") or []:
        attrs[t["name"]] = _make_tool(t)
    return type(f"Http_{spec['id']}", (HttpConnector,), attrs)
