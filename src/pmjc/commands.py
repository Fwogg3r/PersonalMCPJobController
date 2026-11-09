"""Running commands without blocking the model: ssh_run, ssh_check and pmjc_shell_run share this runner.

- The command runs as a subprocess with reader threads. We wait for the DIRECT process to exit, then give the output
  readers a short grace period. If a pipe is still open then, something the command started (a window opened with
  `start`, a daemon) inherited it and is still running: we return what was read with `detached_children: true`
  and close our end of the pipes, so nothing waits for that child.
- A command that takes longer than `wait_seconds` (argument, default config `commands.wait_seconds`) returns at once
  with `{"status": "running", "run_id": ...}` and keeps running in the background, still bound by its timeout. The
  model follows up with `command_status(run_id)`.
- Running commands live in an in-process registry (live output, only in the process that runs the command) and in the
  `command_runs` table (status, exit code, output tail), so another process can see how it ended.
- Only the caller that started a command (or an admin) may see it, like approvals.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import secrets
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import config, db

TAIL_CHARS = 4000  # output kept in the database and shown as output_so_far
COMBINED_KEEP_BYTES = 200_000  # live combined output kept per run (older output is dropped)
READER_GRACE_SECONDS = 1.0
KEEP_FINISHED = 50  # finished runs kept in memory
MAX_STATUS_WAIT = 50

# Who the command belongs to. Set by the executor around every tool call (the requester, for approved ones).
current_owner: contextvars.ContextVar[str] = contextvars.ContextVar("pmjc_command_owner", default="")

_runs: dict[str, "Run"] = {}
_reg_lock = threading.Lock()


def wait_default() -> int:
    return max(0, int(config.load().commands.wait_seconds))


def _flags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil
        try:
            for ch in psutil.Process(proc.pid).children(recursive=True):
                try:
                    ch.kill()
                except Exception:
                    pass
        except Exception:
            pass
    except ImportError:
        pass
    try:
        proc.kill()
    except Exception:
        pass


class Run:
    def __init__(self, tool: str, machine: str, command: str, owner: str, timeout: float, merge: bool,
                 head_cap_bytes: int | None, stream_keep: dict[str, int], finish: Callable[["Run"], dict]):
        self.id = secrets.token_urlsafe(9)
        self.tool, self.machine, self.command, self.owner = tool, machine, command, owner
        self.timeout, self.merge, self.head_cap, self.stream_keep, self._finish = timeout, merge, head_cap_bytes, stream_keep, finish
        self.started = time.time()
        self.lock = threading.Lock()
        self.streams = {"out": bytearray(), "err": bytearray()}
        self.comb = bytearray()
        self.comb_dropped = 0
        self.cursor = 0  # absolute offset in the combined output already handed out by command_status
        self.total_bytes = 0
        self.capped = False
        self.status = "running"  # running | done
        self.exit_code: int | None = None
        self.timed_out = False
        self.detached = False
        self.finished: float | None = None
        self.result: dict | None = None
        self.proc: subprocess.Popen | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.done_evt: asyncio.Event | None = None
        self.exited_evt: asyncio.Event | None = None
        self.readers_evt: asyncio.Event | None = None
        self._readers_left = 0
        self.stop_requested = False

    # -- called from reader threads
    def feed(self, stream: str, data: bytes) -> None:
        kill = False
        with self.lock:
            s = self.streams[stream]
            if self.head_cap is not None:
                if self.total_bytes < self.head_cap:
                    s.extend(data[:self.head_cap - self.total_bytes])
                self.total_bytes += len(data)
                if self.total_bytes > self.head_cap and not self.capped:
                    self.capped = kill = True
            else:
                s.extend(data)
                keep = self.stream_keep.get(stream, 20000) * 4
                if len(s) > keep * 2:
                    del s[:len(s) - keep]
            self.comb.extend(data)
            if len(self.comb) > COMBINED_KEEP_BYTES * 2:
                drop = len(self.comb) - COMBINED_KEEP_BYTES
                del self.comb[:drop]
                self.comb_dropped += drop
        if kill and self.proc:
            kill_tree(self.proc)

    def _signal(self, evt: asyncio.Event | None) -> None:
        if evt is None or self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(evt.set)
        except RuntimeError:
            pass  # loop closed

    def _reader(self, stream: str, f) -> None:
        try:
            while True:
                data = f.read(4096)
                if not data:
                    break
                self.feed(stream, data)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self._readers_left -= 1
                last = self._readers_left <= 0
            if last:
                self._signal(self.readers_evt)

    def text(self, stream: str = "out") -> str:
        with self.lock:
            return bytes(self.streams[stream]).decode("utf-8", errors="replace")

    def tail(self, n: int = TAIL_CHARS) -> str:
        with self.lock:
            return bytes(self.comb[-n * 4:]).decode("utf-8", errors="replace")[-n:]

    def new_output(self, limit: int = 20000) -> tuple[str, bool]:
        """Combined output since the last call (marks it handed out). Returns (text, skipped_older)."""
        with self.lock:
            end = self.comb_dropped + len(self.comb)
            start = max(self.cursor, self.comb_dropped)
            skipped = self.cursor < self.comb_dropped
            chunk = bytes(self.comb[start - self.comb_dropped:])
            if len(chunk) > limit * 4:
                chunk, skipped = chunk[-limit * 4:], True
            self.cursor = end
        t = chunk.decode("utf-8", errors="replace")
        if len(t) > limit:
            t, skipped = t[-limit:], True
        return t, skipped

    def has_new(self) -> bool:
        with self.lock:
            return self.comb_dropped + len(self.comb) > self.cursor

    def seconds(self) -> float:
        return round((self.finished or time.time()) - self.started, 2)

    def info(self) -> dict:
        d = {"run_id": self.id, "status": self.status, "seconds": self.seconds()}
        if self.status == "done":
            d["exit_code"] = self.exit_code
            d["timed_out"] = self.timed_out
            if self.detached:
                d["detached_children"] = True
        return d


def _save(run: Run) -> None:
    try:
        db.x("INSERT INTO command_runs(run_id,tool,machine,command,owner,started,status,output_tail) VALUES(?,?,?,?,?,?,?,?) "
             "ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, output_tail=excluded.output_tail",
             run.id, run.tool, run.machine, run.command[:700], run.owner, run.started, run.status, run.tail())
    except Exception:
        pass


def _save_done(run: Run) -> None:
    try:
        db.x("UPDATE command_runs SET status='done', exit_code=?, output_tail=?, finished=?, detached=?, timed_out=? WHERE run_id=?",
             run.exit_code, run.tail(), run.finished, int(run.detached), int(run.timed_out), run.id)
    except Exception:
        pass


async def _supervise(run: Run) -> None:
    proc = run.proc
    try:
        try:
            await asyncio.wait_for(run.exited_evt.wait(), run.timeout)
        except asyncio.TimeoutError:
            run.timed_out = True
            kill_tree(proc)
            try:
                await asyncio.wait_for(run.exited_evt.wait(), 5)
            except asyncio.TimeoutError:
                pass
        try:
            await asyncio.wait_for(run.readers_evt.wait(), READER_GRACE_SECONDS)
        except asyncio.TimeoutError:
            run.detached = True  # something the command started still holds the pipes
            _close_pipes(proc)
        run.exit_code = proc.returncode
        run.finished = time.time()
        try:
            run.result = run._finish(run)
        except Exception as e:  # never leave a run stuck as running
            run.result = {"error": f"could not build the result: {e!r}"}
        run.status = "done"
        _save_done(run)
    finally:
        run.status = "done"
        if run.finished is None:
            run.finished = time.time()
        run.done_evt.set()
        _prune()


def _close_pipes(proc: subprocess.Popen) -> None:
    """Close our end of the pipes from helper threads (closing can block while a reader thread is mid-read on
    Windows); the blocked reader then ends with the child that holds the other end."""
    for f in (proc.stdout, proc.stderr):
        if f is not None:
            threading.Thread(target=lambda f=f: _quiet_close(f), daemon=True).start()


def _quiet_close(f) -> None:
    try:
        f.close()
    except Exception:
        pass


def _prune() -> None:
    with _reg_lock:
        done = sorted((r for r in _runs.values() if r.status == "done"), key=lambda r: r.finished or 0)
        for r in done[:-KEEP_FINISHED]:
            _runs.pop(r.id, None)


async def execute(*, tool: str, machine: str, argv: list[str], command: str, timeout: float,
                  wait_seconds: float | None, finish: Callable[[Run], dict], cwd: str | None = None, merge: bool = False,
                  head_cap_bytes: int | None = None, stream_keep: dict[str, int] | None = None) -> dict:
    """Start a command and return its finished result (built by `finish(run)`), or, when it is still running after
    `wait_seconds`, a `running` payload with a run id. Raises OSError if the process can't start."""
    wait = wait_default() if wait_seconds is None else max(0.0, float(wait_seconds))
    run = Run(tool, machine, command, current_owner.get(), timeout, merge, head_cap_bytes, stream_keep or {}, finish)
    run.loop = asyncio.get_running_loop()
    run.done_evt, run.exited_evt, run.readers_evt = asyncio.Event(), asyncio.Event(), asyncio.Event()
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT if merge else subprocess.PIPE, creationflags=_flags(), bufsize=0)
    run.proc = proc
    pipes = [("out", proc.stdout)] + ([] if merge else [("err", proc.stderr)])
    run._readers_left = len(pipes)
    for name, f in pipes:
        threading.Thread(target=run._reader, args=(name, f), daemon=True).start()

    def waiter() -> None:
        proc.wait()
        run._signal(run.exited_evt)
    threading.Thread(target=waiter, daemon=True).start()
    with _reg_lock:
        _runs[run.id] = run
    _save(run)
    run.task = asyncio.create_task(_supervise(run))  # kept referenced by the run, which the registry holds
    try:
        await asyncio.wait_for(run.done_evt.wait(), wait)
    except asyncio.TimeoutError:
        _save(run)
        return {"status": "running", "run_id": run.id, "seconds": run.seconds(), "output_so_far": run.tail(),
                "next": "The command is still running. Call command_status(run_id) to get more output or wait for the end."}
    return run.result or {}


# ---- command_status / command_stop ---------------------------------------------------------------------------
class NotVisible(Exception):
    pass


def _allowed(run_owner: str, caller) -> bool:
    if caller is not None:
        return caller.can("admin") or run_owner == caller.name
    return run_owner == current_owner.get()  # internal call (no caller): same owner context only


def _from_db(run_id: str) -> dict | None:
    r = db.q("SELECT * FROM command_runs WHERE run_id=?", run_id)
    return r[0] if r else None


async def status(run_id: str, wait_seconds: float = 20, caller=None) -> dict:
    wait = max(0.0, min(float(wait_seconds), MAX_STATUS_WAIT))
    with _reg_lock:
        run = _runs.get(run_id)
    if run is None:
        row = _from_db(run_id)
        if row is None or not _allowed(row["owner"], caller):
            return {"status": "unknown", "run_id": run_id}
        if row["status"] == "running" and wait:  # another process runs it: poll the table
            end = time.monotonic() + wait
            while time.monotonic() < end and row["status"] == "running":
                await asyncio.sleep(0.5)
                row = _from_db(run_id) or row
        out = {"run_id": run_id, "status": row["status"], "seconds": round((row["finished"] or time.time()) - row["started"], 2),
               "output_tail": row["output_tail"] or "",
               "note": "this command runs in another process: only its status and last output are visible here"}
        if row["status"] == "done":
            out.update(exit_code=row["exit_code"], timed_out=bool(row["timed_out"]))
            if row["detached"]:
                out["detached_children"] = True
        elif time.time() - row["started"] > 3600 * 6:
            out["status"] = "lost"
        return out
    if not _allowed(run.owner, caller):
        return {"status": "unknown", "run_id": run_id}
    end = time.monotonic() + wait
    while run.status == "running" and not run.has_new() and time.monotonic() < end:
        await asyncio.sleep(0.05)
    settle = time.monotonic() + 0.3  # output just arrived: a command about to end reports done in this same call
    while run.status == "running" and wait and time.monotonic() < settle:
        await asyncio.sleep(0.05)
    out = run.info()
    text, skipped = run.new_output()
    out["output"] = text
    if skipped:
        out["output_skipped_older"] = True
    return out


async def stop(run_id: str, caller=None) -> dict:
    with _reg_lock:
        run = _runs.get(run_id)
    if run is None:
        return {"status": "unknown", "run_id": run_id, "note": "not running in this process"}
    if not _allowed(run.owner, caller):
        return {"status": "unknown", "run_id": run_id}
    if run.status != "running":
        return run.info()
    run.stop_requested = True
    kill_tree(run.proc)
    try:
        await asyncio.wait_for(run.done_evt.wait(), 6)
    except asyncio.TimeoutError:
        pass
    return {**run.info(), "stopped": True}


def reset() -> None:
    """Forget in-process state (tests)."""
    with _reg_lock:
        _runs.clear()
