"""`pmjc eval smoke`: drive every real tool with a model, in plain language, without changing anything.

The eval suite (runner.py) uses fake connectors. The smoke test uses the owner's real connectors instead, to see
whether a model picks the right tool and fills its arguments for each one:
- **Reads run for real** (Spotify now playing, a folder listing, a web search, an SSH machine list).
- **Acts never run.** Every tool with scope `act` (and every tool of a mounted MCP server unless the owner marked it
  read) gets its function replaced by a recorder that only stores the arguments and returns `{"dry_run": true}`.
  The executor is unchanged, so scopes, taint, levels and approvals all apply as usual: an act still has to be
  proposed for approval, and the approval is then auto-decided inside the temporary home, so only the recorder runs.
- **A temporary data folder** holds a copy of the owner's settings and own connectors (`connectors.d`, `plugins`), minus everything that reaches people
  (`notify_channels`, the Discord front door, approval follow-ups, scheduled jobs, monitoring, standing grants), and
  a copy of the credentials. Refreshed OAuth tokens are written back to the real store afterwards, because some
  services rotate the refresh token and the old one stops working.

Prompts live in `smoke/<connector>.yaml` (the eval task format, generic, no personal data). `pmjc eval smoke
--coverage` lists real tools that have no prompt yet. The dry-run wrapping happens only here: `DryConnector` is built
only by `live_home`, which refuses to run on the real data folder (a test scans the source for other users).
"""
from __future__ import annotations

import contextlib
import functools
import inspect
import os
import secrets as _secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .. import config, creds, db, memory, registry
from ..config import ProviderConfig
from . import tasks

SMOKE_DIR = Path(__file__).parent / "smoke"
RECORDED: list[dict] = []  # acts the run proposed and that were "executed" by a recorder after auto-approval


def _recorder(name: str, fn):
    @functools.wraps(fn)  # keeps the signature, docstring and schema the model sees
    async def rec(*args, **kwargs):
        kwargs.pop("_caller", None)
        RECORDED.append({"tool": name, "args": kwargs})
        return {"dry_run": True, "tool": name, "note": "smoke test: recorded, not executed"}
    return rec


class DryConnector:
    """A real connector whose act tools only record their arguments. Everything else is the real connector."""

    def __init__(self, real) -> None:
        object.__setattr__(self, "_real", real)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def tools(self):
        for meta, fn in self._real.tools():
            if meta["scope"] == "act" or meta.get("writes_state"):
                yield meta, _recorder(meta["name"], fn)
            else:
                yield meta, fn


def _real_secrets() -> dict[str, str]:
    out = {}
    try:
        st = creds.store()
        for r in db.q("SELECT k FROM secrets"):
            v = st.get(r["k"])
            if v is not None:
                out[r["k"]] = v
    except Exception:
        pass
    return out


def smoke_settings(real: config.Settings, provider: ProviderConfig) -> config.Settings:
    """The owner's settings with one model provider and nothing that reaches a person or runs on its own."""
    s = real.model_copy(deep=True)
    s.routing = [provider]
    s.notify_channels = []
    s.discord_door.enabled = False
    s.approval_outcome_notify = False
    s.scheduled_jobs_enabled = False
    s.monitoring_enabled = False
    s.grants = []
    s.credential_backend = "file"
    return s


@contextlib.contextmanager
def live_home(provider: ProviderConfig) -> Iterator[Path]:
    """A temporary PMJC_HOME with the owner's connectors (acts dry-run) and a copy of their credentials."""
    from .runner import _reset_singletons
    real_home = config.home()
    real_settings = config.load()
    original = _real_secrets()
    saved = {k: os.environ.get(k) for k in ("PMJC_HOME", "PMJC_MASTER_KEY")}
    master = os.environ.get("PMJC_MASTER_KEY")
    changed: dict[str, str] = {}
    _reset_singletons()
    try:
        with tempfile.TemporaryDirectory(prefix="pmjc-smoke-", ignore_cleanup_errors=True) as d:
            if Path(d).resolve() == Path(real_home).resolve():
                raise RuntimeError("smoke test refused: the temporary home is the real data folder")
            os.environ["PMJC_HOME"] = d
            os.environ["PMJC_MASTER_KEY"] = _secrets.token_hex(16)
            try:
                config.save(smoke_settings(real_settings, provider))
                for k, v in original.items():
                    creds.store().set(k, v)
                for sub in ("connectors.d", "plugins"):  # the owner's own connectors (definitions, no secrets)
                    if (Path(real_home) / sub).is_dir():
                        shutil.copytree(Path(real_home) / sub, Path(d) / sub, dirs_exist_ok=True)
                from .. import watches
                db.x(watches.DDL)
                reg = registry.Registry().load()
                reg.connectors = {cid: DryConnector(c) for cid, c in reg.connectors.items()}
                registry._reg = reg
                RECORDED.clear()
                yield Path(d)
            finally:
                try:
                    now = _real_secrets()
                    changed = {k: v for k, v in now.items() if k in original and v != original[k]}
                except Exception:
                    changed = {}
                for c in list((registry._reg.connectors if registry._reg else {}).values()):
                    with contextlib.suppress(Exception):
                        close = getattr(c, "aclose", None) or getattr(c, "close", None)
                        if close and not inspect.iscoroutinefunction(close):
                            close()
                _reset_singletons()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if master is None:
            os.environ.pop("PMJC_MASTER_KEY", None)
        _reset_singletons()
        if changed:  # rotated OAuth tokens: keep the real store working
            for k, v in changed.items():
                creds.store().set(k, v)


def owner_dir() -> Path:
    """Prompts for the owner's own tools (plugins, mounted MCP servers): <data folder>/evals/smoke/*.yaml."""
    return config.home() / "evals" / "smoke"


def load_tasks(connectors: list[str] | None = None) -> list[tasks.Task]:
    files = sorted(SMOKE_DIR.glob("*.y*ml"))
    if owner_dir().is_dir():
        files += sorted(owner_dir().glob("*.y*ml"))
    sets = [tasks.load_file(p) for p in files]
    if connectors:
        sets = [s for s in sets if s.set in connectors]
    return tasks.all_tasks(sets)


def coverage(provider: ProviderConfig) -> dict[str, list[str]]:
    """Real tools without a smoke prompt (by connector), counting a tool as covered when a prompt expects it."""
    covered = set()
    for t in load_tasks():
        covered |= set(t.expect.tools_called) | {a.tool for a in t.expect.acts}
    with live_home(provider):
        missing: dict[str, list[str]] = {}
        for t in registry.get().tools():
            if t.name not in covered:
                missing.setdefault(t.connector, []).append(t.name)
    return missing
