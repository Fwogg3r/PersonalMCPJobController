"""Command connectors: wrap command-line tools you already use as pmjc tools, with YAML only.

Put a file in PMJC_HOME/connectors.d/ with `kind: command`:

    kind: command
    id: mytools
    name: My tools
    description: Roadmaps and model status.
    cwd: C:/Users/me                       # optional working directory
    timeout: 60                            # seconds (per tool override: timeout)
    tools:
      - name: mytools_roadmap
        description: List a project's roadmap.
        scope: read                        # read | act (act needs approval); default act
        # level: auto_clean                # optional permission level (off | always_ask | ask | auto_clean | auto)
        # group: system                    # optional tool group (default: the file's `group:`, else commands)
        untrusted_output: false            # default true
        argv: [roadmap, list, --json, --dir, "{project}/docs/roadmap"]
        params:
          project: {type: string, description: Project folder, pattern: "^[A-Za-z]:[\\\\/][^\"<>|]*$"}

Safety: commands run as argv (never through a shell), so arguments can't inject other commands. Each parameter
can carry a regex `pattern` and `choices`; values that start with "-" are refused unless `allow_dash: true`.
Command files are read from disk only, never created or edited through the API.
"""
from __future__ import annotations

import asyncio
import inspect
import keyword
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .connector import Connector, Field, Health, tool

_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool}


def _pyname(name: str) -> str:
    n = re.sub(r"\W", "_", name)
    return n if n.isidentifier() and not keyword.iskeyword(n) else f"p_{n}"


def _make_tool(spec: dict) -> Any:
    params: dict = spec.get("params") or {}
    sig = []
    for wire, p in params.items():
        ann = _TYPES.get(p.get("type", "string"), str)
        required = p.get("required", "default" not in p)
        default = inspect.Parameter.empty if required else p.get("default")
        if not required and default is None:
            ann = ann | None
        sig.append(inspect.Parameter(_pyname(wire), inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
    sig.sort(key=lambda x: x.default is not inspect.Parameter.empty)

    async def fn(self, **kwargs):
        return await self._run(spec, {w: kwargs.get(_pyname(w)) for w in params})

    fn.__name__ = spec["name"]
    doc = spec.get("description", "")
    for w, p in params.items():
        extra = p.get("description", "")
        if p.get("choices"):
            extra += f" One of: {', '.join(map(str, p['choices']))}."
        if extra:
            doc += f"\n{_pyname(w)}: {extra}"
    fn.__doc__ = doc
    fn.__signature__ = inspect.Signature([inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD), *sig],
                                         return_annotation=Any)
    fn.__annotations__ = {p.name: p.annotation for p in sig} | {"return": Any}
    return tool(fn, scope=spec.get("scope", "act"), untrusted_output=spec.get("untrusted_output", True),
                group=spec.get("group"), fixed_host=bool(spec.get("fixed_host", False)),
                egress=spec.get("egress", False), level=spec.get("level"))


class CommandConnector(Connector):
    spec: dict = {}

    def _value(self, name: str, p: dict, v: Any) -> str:
        s = str(v).lower() if isinstance(v, bool) else str(v)
        if p.get("choices") and v not in p["choices"]:
            raise ValueError(f"{name} must be one of {p['choices']}")
        if p.get("pattern") and not re.fullmatch(p["pattern"], s):
            raise ValueError(f"{name} doesn't match the allowed pattern")
        if s.startswith("-") and not p.get("allow_dash"):
            raise ValueError(f"{name} may not start with '-'")
        if "\x00" in s or "\n" in s:
            raise ValueError(f"{name} contains a control character")
        return s

    def _argv(self, spec: dict, args: dict) -> list[str]:
        params = spec.get("params") or {}
        values = {}
        for name, p in params.items():
            v = args.get(name)
            if v is None:
                v = p.get("default")
            if v is None:
                if p.get("required", "default" not in p):
                    raise ValueError(f"{name} is required")
                continue
            values[name] = self._value(name, p, v)
        out = []
        for part in spec["argv"]:
            names = re.findall(r"\{([^}]+)\}", part)
            if any(n not in values for n in names):
                if part.strip().startswith("{") and part.strip().endswith("}") and len(names) == 1:
                    continue  # optional parameter that wasn't given: drop the whole argument
                raise ValueError(f"missing value for {names}")
            for n in names:
                part = part.replace("{" + n + "}", values[n])
            out.append(os.path.expandvars(os.path.expanduser(part)))
        return out

    async def _run(self, spec: dict, args: dict) -> dict:
        argv = self._argv(spec, args)
        timeout = int(spec.get("timeout", self.spec.get("timeout", 60)))
        cwd = self.spec.get("cwd")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        env.update(self.spec.get("env") or {})

        def run():
            return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, cwd=os.path.expandvars(cwd) if cwd else None, env=env,
                                  creationflags=flags, stdin=subprocess.DEVNULL)
        try:
            r = await asyncio.to_thread(run)
        except subprocess.TimeoutExpired:
            return {"exit_code": None, "error": f"timed out after {timeout}s"}
        except FileNotFoundError:
            return {"exit_code": None, "error": f"command not found: {argv[0]}"}
        limit = int(spec.get("max_output", 20000))
        return {"exit_code": r.returncode, "stdout": r.stdout[-limit:], "stderr": r.stderr[-4000:]}

    async def health(self) -> Health:
        exes = {t["argv"][0] for t in self.spec.get("tools") or []}
        import shutil
        missing = [e for e in exes if not (shutil.which(os.path.expandvars(e)) or Path(os.path.expandvars(e)).exists())]
        return Health(not missing, "ready" if not missing else f"not found: {', '.join(missing)}")


def load_command_connector(file: Path) -> type[Connector]:
    spec = yaml.safe_load(file.read_text(encoding="utf-8"))
    attrs: dict[str, Any] = {
        "id": spec["id"], "name": spec.get("name", spec["id"]), "description": spec.get("description", ""),
        "docs_url": spec.get("docs_url", ""), "spec": spec,
        "limitations": spec.get("limitations", ""), "native_alternatives": spec.get("native_alternatives", ""),
        "credentials": [Field(**c) for c in spec.get("credentials") or []],
        "group": spec.get("group", "commands"),
        "platforms": spec.get("platforms"),
    }
    for t in spec.get("tools") or []:
        attrs[t["name"]] = _make_tool(t)
    return type(f"Cmd_{spec['id']}", (CommandConnector,), attrs)
