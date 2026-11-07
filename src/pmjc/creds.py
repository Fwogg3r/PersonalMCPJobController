"""Credential store (envelope encryption).

Secrets are encrypted with one random data key and kept in the pmjc database (no size limits, no per-item
OS prompts). Only the data key lives in the OS vault:
  keyring (default): Windows Credential Manager / macOS Keychain / Secret Service
  file: derived from PMJC_MASTER_KEY (headless Linux; prefer systemd-creds to inject it)
Env always wins and is read-only: PMJC_<CONNECTOR>_<FIELD>, e.g. PMJC_DISCORD_BOT_TOKEN.

Threat model: this protects secrets at rest and from the repo. Any process running as the same OS user can
still read them (as with any keyring or browser profile).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
import time

from cryptography.fernet import Fernet

from . import config, db

SERVICE = "pmjc"
_DDL = "CREATE TABLE IF NOT EXISTS secrets (k TEXT PRIMARY KEY, v BLOB, updated REAL)"


def env_name(key: str) -> str:
    return "PMJC_" + re.sub(r"[^A-Z0-9]", "_", key.upper())


def _data_key(backend: str) -> bytes:
    if backend == "file":
        secret = os.environ.get("PMJC_MASTER_KEY")
        if not secret:
            raise RuntimeError("credential_backend 'file' needs PMJC_MASTER_KEY in the environment")
        return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    if backend == "env":
        return Fernet.generate_key()  # nothing is stored
    import keyring
    k = keyring.get_password(SERVICE, "data-key")
    if not k:
        k = Fernet.generate_key().decode()
        keyring.set_password(SERVICE, "data-key", k)
    return k.encode()


class Store:
    def __init__(self, backend: str | None = None) -> None:
        self.name = backend or config.load().credential_backend
        self.f = Fernet(_data_key(self.name))
        db.x(_DDL)
        self._locks: dict[str, threading.Lock] = {}

    def lock(self, key: str) -> threading.Lock:
        """Per-credential lock, e.g. around OAuth refresh with rotating refresh tokens."""
        return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str) -> str | None:
        v = os.environ.get(env_name(key))
        if v:
            return v
        rows = db.q("SELECT v FROM secrets WHERE k=?", key)
        if not rows:
            return None
        val = self.f.decrypt(rows[0]["v"]).decode()
        Redactor.add(val)
        return val

    def set(self, key: str, value: str) -> None:
        if self.name == "env":
            raise RuntimeError(f"credential_backend is 'env' (read-only); set {env_name(key)}")
        db.x("INSERT INTO secrets(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
             key, self.f.encrypt(value.encode()), time.time())
        Redactor.add(value)

    def delete(self, key: str) -> None:
        db.x("DELETE FROM secrets WHERE k=?", key)

    def has(self, key: str) -> bool:
        return bool(os.environ.get(env_name(key))) or bool(db.q("SELECT 1 FROM secrets WHERE k=?", key))

    def keys(self) -> list[str]:
        return [r["k"] for r in db.q("SELECT k FROM secrets ORDER BY k")]

    def load_all_for_redaction(self) -> None:
        for k in self.keys():
            self.get(k)


class Redactor(logging.Filter):
    """Removes every known secret value from log records and exception text."""
    values: set[str] = set()

    @classmethod
    def add(cls, v: str | None) -> None:
        if v and len(v) >= 6:
            cls.values.add(v)

    @classmethod
    def scrub(cls, text: str) -> str:
        for v in sorted(cls.values, key=len, reverse=True):
            text = text.replace(v, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg, record.args = self.scrub(msg), None
        if record.exc_info and record.exc_info[1] is not None:
            import traceback
            record.exc_text = self.scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store
