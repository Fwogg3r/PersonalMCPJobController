"""Client tokens and scopes.

Scopes: sources (read tools), actions (side-effect tools; still need approval), jobs (create/run jobs),
admin (dashboard, settings, credentials, approvals). Tokens are stored hashed; the plain value is shown once.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config, db

ALL_SCOPES = ["sources", "actions", "jobs", "admin"]


@dataclass
class Caller:
    name: str
    kind: str  # local | admin | remote | job
    scopes: set[str] = field(default_factory=set)
    untrusted: bool = False  # the run has read third-party content (dual-LLM rule)
    auto_approve: set[str] = field(default_factory=set)
    allowed_tools: set[str] | None = None  # None = every tool the scopes allow
    thread_id: str | None = None  # the thread this run belongs to (act ledger, approvals)
    acted: bool = False  # an act tool has executed in this run (no provider fallback after this)
    read_urls: tuple[str, ...] = ()  # owner-declared exact URLs a browser read may fetch even after taint
    read_sites: tuple[str, ...] = ()  # owner-declared domains (and subdomains) likewise; set only from stored job/watch definitions
    read_prefixes: tuple[str, ...] = ()  # owner-declared URL prefixes (scheme+host+path start) likewise; stored definitions only
    sensitive: bool = False  # the run has received the user's private data (model.md, notes, thread history, private tool
    # results). Derived and fail-closed, like `untrusted`. Only tracked for now: a later research sub-run (web tools only,
    # nothing private in its prompt) will be refused once its parent is sensitive, so private data can't ride into web queries.
    quarantine: bool = False  # a research sub-run (pmjc.research): holds nothing private, so egress after untrusted content is
    # allowed while `sensitive` stays False. Set ONLY by research.new_caller(); no API, model, job or watch path sets it.
    approved_write: bool = False  # internal: the owner approved a state write proposed by a tainted run; it is stored untrusted
    seen_urls: set[str] = field(default_factory=set, repr=False, compare=False)  # URLs the run's tools returned or fetched
    on_tool: Callable | None = field(default=None, repr=False, compare=False)  # async (name, args, status); trace hook
    surface: str = ""  # discord | rest | cli | v1 | job: where the request came from (front-door routing)
    tier_hint: str = ""  # a tier the caller chose explicitly (e.g. /v1 model "pmjc:strong")

    def can(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes

    @property
    def needs_approval(self) -> bool:
        # every agent-facing interface (stdio MCP included) needs approval for act tools;
        # only a person at the CLI or the dashboard runs them directly
        return self.kind != "admin"


def local() -> Caller:
    """An agent on this machine (MCP stdio). Callers carry per-session taint, so make a new one per session."""
    return Caller("local", "local", set(ALL_SCOPES))


def admin(name: str = "admin") -> Caller:
    """A person at the CLI or dashboard."""
    return Caller(name, "admin", set(ALL_SCOPES))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(name: str, scopes: list[str]) -> str:
    bad = set(scopes) - set(ALL_SCOPES)
    if bad:
        raise ValueError(f"unknown scopes: {sorted(bad)}")
    token = "pmjc_" + secrets.token_urlsafe(32)
    db.x("INSERT INTO tokens(id,name,hash,scopes,created) VALUES(?,?,?,?,?)",
         secrets.token_hex(6), name, _hash(token), json.dumps(sorted(scopes)), time.time())
    return token


_run_callers: dict[str, Caller] = {}


def issue_run_token(caller: Caller) -> str:
    """Short-lived, in-memory token that lets a subprocess agent (claude -p) act as this exact caller."""
    token = "pmjc_run_" + secrets.token_urlsafe(32)
    _run_callers[_hash(token)] = caller
    return token


def drop_run_token(token: str) -> None:
    _run_callers.pop(_hash(token), None)


def verify(token: str) -> Caller | None:
    run = _run_callers.get(_hash(token))
    if run is not None:
        return run
    rows = db.q("SELECT * FROM tokens WHERE hash=? AND revoked=0", _hash(token))
    if not rows:
        return None
    r = rows[0]
    db.x("UPDATE tokens SET last_used=? WHERE id=?", time.time(), r["id"])
    scopes = set(json.loads(r["scopes"]))
    # always "remote": an agent holding an admin token still goes through approval for act tools;
    # the dashboard explicitly switches to an admin caller for a person's own clicks
    return Caller(f"token:{r['name']}", "remote", scopes)


def list_tokens() -> list[dict]:
    return [{k: v for k, v in r.items() if k != "hash"} for r in db.q("SELECT * FROM tokens ORDER BY created DESC")]


def revoke(token_id: str | None = None) -> int:
    """Revoke one token, or all of them (the kill switch) when token_id is None."""
    if token_id:
        return db.x("UPDATE tokens SET revoked=1 WHERE id=?", token_id)
    return db.x("UPDATE tokens SET revoked=1")


def oauth_scopes(identity: str | None) -> set[str]:
    if not identity:
        return set()
    allowed = config.load().oauth.allowed
    return set(allowed.get(identity.lower(), allowed.get(identity, [])))


def signing_key() -> bytes:
    k = db.kv_get("hmac_key")
    if not k:
        k = secrets.token_hex(32)
        db.kv_set("hmac_key", k)
    return bytes.fromhex(k)


def sign(msg: str) -> str:
    return hmac.new(signing_key(), msg.encode(), hashlib.sha256).hexdigest()


def check_sig(msg: str, sig: str) -> bool:
    return hmac.compare_digest(sign(msg), sig or "")
