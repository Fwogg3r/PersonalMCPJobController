"""SQLite state: events, job runs, approvals, client tokens, audit log, model spend."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, key TEXT UNIQUE, source TEXT, kind TEXT, title TEXT, text TEXT,
  data TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, job TEXT, trigger TEXT, status TEXT, provider TEXT, started REAL, finished REAL,
  output TEXT, error TEXT, cost_usd REAL DEFAULT 0, thread_id TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, tool TEXT, args TEXT, payload_hash TEXT, requested_by TEXT, reason TEXT,
  status TEXT, created REAL, expires REAL, decided REAL, result TEXT, thread_id TEXT, tainted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tokens (
  id TEXT PRIMARY KEY, name TEXT, hash TEXT UNIQUE, scopes TEXT, created REAL, last_used REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, ts REAL, actor TEXT, tool TEXT, args_hash TEXT, status TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS spend (
  id INTEGER PRIMARY KEY, ts REAL, provider TEXT, usd REAL, tokens_in INTEGER, tokens_out INTEGER);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(config.path("pmjc.db"), timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        # Rollback journal: a few processes (service, CLI) share this small file; simple locking, no -wal/-shm files.
        c.execute("PRAGMA journal_mode=DELETE")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=FULL")
        c.executescript(SCHEMA)
        for t in ("runs", "approvals"):  # databases created before threads existed
            if "thread_id" not in {r[1] for r in c.execute(f"PRAGMA table_info({t})")}:
                c.execute(f"ALTER TABLE {t} ADD COLUMN thread_id TEXT")
        if "tainted" not in {r[1] for r in c.execute("PRAGMA table_info(approvals)")}:
            c.execute("ALTER TABLE approvals ADD COLUMN tainted INTEGER DEFAULT 0")  # the requesting run had read untrusted content
        have = {r[1] for r in c.execute("PRAGMA table_info(approvals)")}
        for col, ddl in (("delivered_to_requester", "INTEGER DEFAULT 0"), ("waiter_seen", "REAL DEFAULT 0")):
            if col not in have:  # outcome delivered straight to the waiting MCP client; waiter heartbeat
                c.execute(f"ALTER TABLE approvals ADD COLUMN {col} {ddl}")
        _local.c = c
    return c


def q(sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def x(sql: str, *args: Any) -> int:
    cur = conn().execute(sql, args)
    return cur.lastrowid or cur.rowcount


def kv_get(k: str, default: Any = None) -> Any:
    r = q("SELECT v FROM kv WHERE k=?", k)
    return json.loads(r[0]["v"]) if r else default


def kv_set(k: str, v: Any) -> None:
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, json.dumps(v))


def audit(actor: str, tool: str, args_hash: str, status: str, detail: str = "") -> None:
    """Metadata only: never message bodies or argument values."""
    x("INSERT INTO audit(ts,actor,tool,args_hash,status,detail) VALUES(?,?,?,?,?,?)",
      time.time(), actor, tool, args_hash, status, detail[:300])


def prune(retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    for t, col in (("audit", "ts"), ("events", "ts"), ("runs", "started"), ("approvals", "created")):
        x(f"DELETE FROM {t} WHERE {col} < ?", cutoff)
