"""Stored tool results for paging (phase4/p).

A tool result longer than the provider's cap is cut in the run and kept whole under a short handle. The model
pages the rest with `pmjc_result_get`. Handles belong to the thread (or, with no thread, the run) that created
them, expire after `result_ttl_hours`, and remember whether the original tool was untrusted so reading a slice
taints the run the same way the original call did.
"""
from __future__ import annotations

import secrets
import time

from . import config, db

PAGE_TOOL = "pmjc_result_get"  # the paging tool (agent.PAGE_TOOL is the same name)

DDL = """
CREATE TABLE IF NOT EXISTS results (
  id TEXT PRIMARY KEY, thread_id TEXT, run_key TEXT, tool TEXT, untrusted INTEGER, ts REAL, text TEXT);
"""


def _init() -> None:
    if not db.q("SELECT 1 FROM sqlite_master WHERE name='results'"):
        db.conn().executescript(DDL)


def run_key(caller) -> str:
    """A stable id for one run's caller object (callers are made per run)."""
    k = getattr(caller, "_pmjc_run_key", None)
    if k is None:
        k = secrets.token_hex(6)
        try:
            caller._pmjc_run_key = k
        except Exception:
            pass
    return k


def prune() -> None:
    _init()
    db.x("DELETE FROM results WHERE ts<?", time.time() - config.load().result_ttl_hours * 3600)


def put(caller, tool: str, text: str, untrusted: bool) -> str:
    _init()
    prune()
    while True:
        rid = "r_" + secrets.token_hex(2)
        if not db.q("SELECT 1 FROM results WHERE id=?", rid):
            break
    db.x("INSERT INTO results(id,thread_id,run_key,tool,untrusted,ts,text) VALUES(?,?,?,?,?,?,?)",
         rid, getattr(caller, "thread_id", None), run_key(caller), tool, int(untrusted), time.time(), text)
    return rid


def get(caller, rid: str, offset: int = 0, max_chars: int = 4000) -> dict:
    """The next slice of a stored result, or {"error": ...}. Taints the caller if the original was untrusted."""
    _init()
    prune()
    rows = db.q("SELECT * FROM results WHERE id=?", str(rid))
    tid = getattr(caller, "thread_id", None)
    r = rows[0] if rows else None
    if r is None or not ((tid and r["thread_id"] == tid) or r["run_key"] == run_key(caller)):
        return {"error": f"no stored result {rid} (unknown, expired, or belongs to another thread)"}
    offset = max(0, int(offset or 0))
    max_chars = max(1, min(int(max_chars or 4000), 20000))
    text = r["text"]
    part = text[offset:offset + max_chars]
    end = offset + len(part)
    if r["untrusted"]:
        caller.untrusted = True  # the slice has the original result's trust
    out = {"id": rid, "tool": r["tool"], "offset": offset, "text": part, "total_chars": len(text),
           "remaining": max(0, len(text) - end)}
    if out["remaining"]:
        out["next"] = f'pmjc_result_get(id="{rid}", offset={end})'
    return out
