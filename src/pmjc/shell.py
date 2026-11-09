"""The approved shell-command fallback (tool `pmjc_shell_run`): last resort when no pmjc tool can do the job.

Rules, all enforced outside the model's control:
- it is an `act` tool and always goes through the approval flow, never auto-approved, even for a person at the CLI;
- a tainted run (one that read third-party content) can't even propose it;
- config `shell_command_proposers` says which runs may propose it ("owner" chats, "jobs", "watches"; empty = off);
- timeout and output cap come from config; the model may ask for other values, clamped to the configured maximums,
  and the effective values are stored in the approval so the owner sees exactly what will run.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config
from .auth import Caller

TOOL = "pmjc_shell_run"
SHELLS = ("auto", "powershell", "cmd", "sh")
MAX_COMMAND_CHARS = 700  # short enough that every approval view (chat, notification, dashboard) shows the whole command


class ShellDenied(Exception):
    pass


def proposer_class(caller: Caller) -> str | None:
    """Which config class a caller belongs to: owner, jobs, watches, or None (never allowed)."""
    if caller.name.startswith("watch:"):
        return "watches"
    if caller.name.startswith("job:"):
        return "jobs"
    if caller.kind == "job":
        return None  # e.g. approval follow-ups: never propose another command
    return "owner"


def resolve_shell(setting: str | None = None) -> str:
    s = (setting or config.load().shell_command_shell or "auto").lower()
    if s not in SHELLS:
        s = "auto"
    if s == "auto":
        return "powershell" if sys.platform == "win32" else "sh"
    return s


def _cwd() -> str:
    c = config.load().shell_command_cwd.strip()
    return str(Path(os.path.expandvars(os.path.expanduser(c)))) if c else str(Path.home())


def _clamp(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


def normalize(args: dict) -> dict:
    """The arguments as stored in the approval: command plus every effective setting. Model-supplied shell and
    working directory are ignored (config decides); timeout and output cap are clamped to the configured maximums."""
    s = config.load()
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ShellDenied("command must be a non-empty string")
    if len(cmd) > MAX_COMMAND_CHARS:
        raise ShellDenied(f"command is over {MAX_COMMAND_CHARS} characters; the owner must be able to read all of it "
                          "in the approval, so propose a shorter one (or several steps)")
    return {"command": cmd,
            "shell": resolve_shell(),
            "cwd": _cwd(),
            "timeout_seconds": _clamp(args.get("timeout_seconds"), s.shell_command_timeout_seconds, 1,
                                      max(s.shell_command_max_timeout_seconds, 1)),
            "max_output_chars": _clamp(args.get("max_output_chars"), s.shell_command_output_cap_chars, 100,
                                       max(s.shell_command_max_output_chars, 100))}


def check_proposal(caller: Caller) -> str | None:
    """None if the caller may propose a command, else the reason it may not."""
    if caller.untrusted:
        return ("this run has read untrusted content, so it can't propose shell commands; "
                "start a fresh run or ask the user")
    cls = proposer_class(caller)
    if cls is None or cls not in set(config.load().shell_command_proposers):
        return f"shell commands may not be proposed by {caller.name} (config shell_command_proposers)"
    return None


def _argv(shell: str, command: str) -> list[str]:
    if shell == "powershell":
        exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command", command]
    if shell == "cmd":
        return [shutil.which("cmd") or "cmd", "/d", "/s", "/c", command]
    return [shutil.which("sh") or "/bin/sh", "-c", command]


async def run(command: str, timeout_seconds: int | None = None, max_output_chars: int | None = None,
              shell: str | None = None, cwd: str | None = None) -> dict:
    """Run an approved command. Re-clamps the limits (hard maximums) and always uses the configured shell and
    working directory (`shell` and `cwd` arguments are accepted only so stored approvals round-trip; ignored).
    stdout and stderr come back together."""
    a = normalize({"command": command, "timeout_seconds": timeout_seconds, "max_output_chars": max_output_chars})
    shell, cwd = a["shell"], a["cwd"]
    timeout, cap = a["timeout_seconds"], a["max_output_chars"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_argv(shell, command), cwd=cwd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, creationflags=flags)
    except OSError as e:
        return {"error": f"could not start: {e}", "shell": shell, "cwd": cwd}
    buf = bytearray()
    capped = False
    byte_cap = cap * 4 + 4  # a char is at most 4 bytes; trimmed by chars below

    async def pump() -> None:
        nonlocal capped
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > byte_cap:
                capped = True
                proc.kill()
                return

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        pass
    text = buf.decode("utf-8", errors="replace")
    return {"exit_code": proc.returncode, "output": text[:cap], "truncated": capped or len(text) > cap,
            "timed_out": timed_out, "seconds": round(time.time() - t0, 2), "shell": shell, "cwd": cwd,
            "timeout_seconds": timeout, "max_output_chars": cap}
