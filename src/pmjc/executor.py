"""Single path for every tool call: scope check, approval for side effects, audit.

MCP, REST, CLI and the job agent all call `call_tool`, so the rules can't be bypassed by picking an interface.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlparse, urlsplit

from . import auth, config, db, defang, memory, registry, results, route_choice, shell
from .auth import Caller

log = logging.getLogger("pmjc.executor")


class Denied(Exception):
    pass


def payload_hash(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode()).hexdigest()


DECLARED_READ_TOOLS = {"browser_read", "browser_extract", "web_fetch"}  # read tools whose url(s) the owner may fix in advance


def _prefix_match(prefix: str, url: str) -> bool:
    """`url` starts with the declared prefix (same scheme and host, path on a segment boundary). A URL with a query
    string matches only a prefix that itself contains one (then it's a plain string prefix); `..` segments never match."""
    try:
        pp, up = urlsplit(prefix), urlsplit(url)
    except ValueError:
        return False
    if up.scheme not in ("http", "https") or up.scheme != pp.scheme or up.netloc.lower() != pp.netloc.lower():
        return False
    if "\\" in url or ".." in up.path.split("/") or "%2e" in up.path.lower():
        return False
    if pp.query:
        return url.startswith(prefix)
    if "?" in url:
        return False
    base = pp.path or "/"
    return up.path == base or up.path.startswith(base if base.endswith("/") else base + "/")


def _url_declared(caller: Caller, url: str) -> bool:
    if url in caller.read_urls:  # exact, query string included
        return True
    if any(_prefix_match(p, url) for p in caller.read_prefixes):
        return True
    p = urlparse(url)
    host = (p.hostname or "").lower()
    # read_sites: any path on the site, but never a query string (it would let the model put data in the URL)
    return (p.scheme in ("http", "https") and "?" not in url
            and any(host == d.lower() or host.endswith("." + d.lower()) for d in caller.read_sites))


def declared_read(caller: Caller, name: str, args: dict) -> bool:
    """True when `name` is a declared-read tool and its url (for web_fetch: every url in `urls`) is one the owner listed
    in the stored job/watch definition: `read_urls` match exactly, `read_prefixes` by URL prefix (a path on one host),
    `read_sites` by domain (subdomains too) but only for URLs without a query string. The owner fixed the URL, so it
    isn't model-chosen egress."""
    if name not in DECLARED_READ_TOOLS or not (caller.read_urls or caller.read_sites or caller.read_prefixes):
        return False
    if name == "web_fetch":
        urls = args.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        return isinstance(urls, list) and bool(urls) and all(isinstance(u, str) and _url_declared(caller, u) for u in urls)
    return _url_declared(caller, str(args.get("url") or ""))


def _remember_urls(caller: Caller, args: dict, res: Any) -> None:
    """URLs a tool returned, and (while the run is still clean) the ones it was asked to fetch, may be shown in the answer."""
    try:
        if not caller.untrusted:
            defang.remember(caller.seen_urls, json.dumps(args, default=str)[:20000])
        defang.remember(caller.seen_urls, json.dumps(res, default=str)[:200000])
    except Exception:
        log.debug("could not record URLs of %s", caller.name, exc_info=True)


def run_flags(caller: Caller) -> str:
    return f"untrusted={int(caller.untrusted)} sensitive={int(caller.sensitive)}"


def audit_run(caller: Caller, what: str) -> None:
    """One audit row at the end of a run with its derived flags (metadata only)."""
    db.audit(caller.name, what, "", "run_end", run_flags(caller))


def _route(res: Any, tid: str | None) -> None:
    """Remember which thread sent a message that can be answered (see memory.reply_route_set)."""
    if isinstance(res, dict) and res.get("reply_route"):
        memory.reply_route_set(str(res["reply_route"]), tid)


async def _invoke(fn, args: dict, caller: Caller | None = None, owner: str | None = None) -> Any:
    from . import commands
    if "_caller" in inspect.signature(fn).parameters:
        args = {**args, "_caller": caller}
    token = commands.current_owner.set(owner if owner is not None else (caller.name if caller else ""))
    try:
        res = fn(**args)
        if inspect.isawaitable(res):
            res = await res
    finally:
        commands.current_owner.reset(token)
    return res


async def call_tool(caller: Caller, name: str, args: dict | None = None, reason: str = "") -> Any:
    """Run a tool through every check. If the caller has an `on_tool` hook it hears (name, args, status), never results."""
    status = "error"
    try:
        res = await _call_tool(caller, name, args, reason)
        status = "pending_approval" if isinstance(res, dict) and res.get("status") == "pending_approval" else "ok"
        return res
    except Denied:
        status = "denied"
        raise
    finally:
        hook = getattr(caller, "on_tool", None)
        if hook:
            try:
                await hook(name, args or {}, status)
            except Exception:
                log.warning("on_tool hook failed", exc_info=True)


async def _call_tool(caller: Caller, name: str, args: dict | None, reason: str) -> Any:
    args = args or {}
    t = registry.get().tool(name)
    if t is None:
        raise Denied(f"unknown tool {name}")
    if name == shell.TOOL:  # approved shell-command fallback: gate and fix the effective limits before hashing
        why = shell.check_proposal(caller)
        if why:
            db.audit(caller.name, name, payload_hash(name, args), "denied", why)
            raise Denied(why)
        try:
            args = shell.normalize(args)
        except shell.ShellDenied as e:
            db.audit(caller.name, name, payload_hash(name, args), "denied", str(e))
            raise Denied(str(e)) from e
    h = payload_hash(name, args)
    if caller.allowed_tools is not None and name not in caller.allowed_tools:
        db.audit(caller.name, name, h, "denied", "not in this run's tool list")
        raise Denied(f"{name} isn't available to {caller.name}")
    need = t.mcp_scope
    if not caller.can(need):
        db.audit(caller.name, name, h, "denied", f"missing scope {need}")
        raise Denied(f"{caller.name} lacks scope '{need}' for {name}")
    quarantined_ok = caller.quarantine and not caller.sensitive  # research sub-run that still holds nothing private
    if t.egress and caller.untrusted and not quarantined_ok and not declared_read(caller, name, args):
        db.audit(caller.name, name, h, "denied", "egress after untrusted content")
        raise Denied(f"{name} sends data off the machine and this run has read untrusted content; "
                     "start a fresh run or ask the user")
    if t.writes_state and caller.untrusted:  # laundering guard: a run that read third-party content can't write trusted state
        return await request_approval(caller, name, args, reason or "writes durable state after the run read untrusted content")
    if t.scope == "act":
        if memory.act_done(caller.thread_id, name, h):  # same act, same payload, same thread: the user decides again
            return await request_approval(caller, name, args, "already done in this thread; approve only if it should repeat")
        auto = (name in caller.auto_approve or name in config.load().auto_approve) and name != shell.TOOL
        if caller.untrusted or name == shell.TOOL or (caller.needs_approval and not auto):  # shell: always approved
            why = reason or ("proposed by a run that read untrusted content" if caller.untrusted else "")
            return await request_approval(caller, name, args, why)
    try:
        res = await _invoke(t.fn, args, caller)
        if t.scope == "act":
            caller.acted = True
            memory.act_record(caller.thread_id, name, h)
            _route(res, caller.thread_id)
        if t.private_output and not (caller.quarantine and name == results.PAGE_TOOL):  # a quarantined run's own paging
            caller.sensitive = True  # derived and fail-closed: only tools declared public-web leave it alone
        _remember_urls(caller, args, res)  # before the taint below: URLs the model chose while still clean
        if t.untrusted_output:
            caller.untrusted = True  # taint is derived, not declared
        db.audit(caller.name, name, h, "ok", run_flags(caller))
        return res
    except Exception as e:
        db.audit(caller.name, name, h, "error", type(e).__name__)
        raise


async def request_approval(caller: Caller, name: str, args: dict, reason: str = "") -> dict:
    tainted = int(bool(caller.untrusted))
    aid = secrets.token_urlsafe(9)
    ttl = config.load().approval_ttl_seconds
    now = time.time()
    db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires,thread_id,tainted) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?)", aid, name, json.dumps(args, default=str), payload_hash(name, args),
         caller.name, reason[:500], "pending", now, now + ttl, caller.thread_id, tainted)
    _thread_log(caller.thread_id, f"approval requested for {name} (id {aid})")
    db.audit(caller.name, name, payload_hash(name, args), "approval_requested", aid)
    if name == shell.TOOL:  # the command itself is audited (it is the owner's to review later)
        db.audit(caller.name, name, payload_hash(name, args), "shell_proposed", f"{aid}: {args.get('command', '')}")
    asyncio.create_task(_announce(aid, name, args, caller.name, reason, bool(tainted), set(caller.seen_urls)))
    return {"status": "pending_approval", "approval_id": aid, "expires_in_seconds": ttl,
            "note": "A person must approve this in the pmjc dashboard or a notification. "
                    "Call pmjc_approval_wait(approval_id) to wait for the decision; check with pmjc_approval_status."}


def _thread_log(tid: str | None, text: str) -> None:
    """Approval outcomes go into the thread: tool name and status only, never arguments or results."""
    if tid and memory.thread_exists(tid):
        memory.thread_add(tid, "approval", text, author="pmjc", trust="owner")


async def _announce(aid: str, name: str, args: dict, by: str, reason: str, tainted: bool = False,
                    seen: set[str] | None = None) -> None:
    preview = defang.text(json.dumps(args, default=str), tainted, seen, wrap=False)  # a tainted run's links can't unfurl
    limit = 1700 if name == shell.TOOL else 600  # a shell command is always shown whole
    if len(preview) > limit:
        preview = preview[:limit] + "..."
    msg = f"{by} wants to run {name}\n{reason}\n{preview}"
    approve = f"/approve/{aid}?sig={auth.sign('approve:' + aid)}"
    deny = f"/approve/{aid}?deny=1&sig={auth.sign('deny:' + aid)}"
    actions = [{"id": "approve", "title": "Approve", "path": approve}, {"id": "deny", "title": "Deny", "path": deny}]
    from . import discord_door
    for ch in registry.get().channels():
        if ch.id == "discord" and by.startswith("discord:") and discord_door.running():
            continue  # the front door already posted buttons in the conversation
        try:
            await ch.notify("pmjc approval needed", msg, actions=actions)
            return
        except Exception as e:  # try the next channel
            log.warning("approval announce via %s failed: %s", ch.id, e)


async def decide(aid: str, approve: bool, decided_by: str) -> dict:
    """Decide an approval (run it or deny it), then wake anything waiting for the decision."""
    try:
        return await _decide(aid, approve, decided_by)
    finally:
        _resolve(aid)


async def _decide(aid: str, approve: bool, decided_by: str) -> dict:
    rows = db.q("SELECT * FROM approvals WHERE id=?", aid)
    if not rows:
        raise Denied("no such approval")
    a = rows[0]
    if a["status"] != "pending":
        return {"status": a["status"], "result": a["result"]}
    if time.time() > a["expires"]:
        _expire(aid, a)
        return {"status": "expired"}
    args = json.loads(a["args"])
    if payload_hash(a["tool"], args) != a["payload_hash"]:
        db.x("UPDATE approvals SET status='tampered' WHERE id=?", aid)
        raise Denied("payload changed after the request")
    # claim it first so it can only run once
    claimed = db.x("UPDATE approvals SET status='running', decided=? WHERE id=? AND status='pending'", time.time(), aid)
    if not claimed:
        return {"status": "already decided"}
    if not approve:
        db.x("UPDATE approvals SET status='denied' WHERE id=?", aid)
        db.audit(decided_by, a["tool"], a["payload_hash"], "denied_by_user", aid)
        _thread_log(a.get("thread_id"), f"{a['tool']} was denied by the user (approval {aid})")
        return {"status": "denied"}
    t = registry.get().tool(a["tool"])
    try:
        if a["tool"] == route_choice.TOOL:  # internal: records the owner's routing choice, runs nothing else
            res = route_choice.decided(a, args)
        else:
            res = await _invoke(t.fn, args, _approved_caller(a, t), owner=a["requested_by"])
        out = json.dumps(res, default=str)[:20000]
        db.x("UPDATE approvals SET status='done', result=? WHERE id=?", out, aid)
        db.audit(decided_by, a["tool"], a["payload_hash"], "approved_ok", aid)
        if a["tool"] == shell.TOOL and isinstance(res, dict):
            db.audit(decided_by, a["tool"], a["payload_hash"], "shell_result",
                     f"{aid}: exit {res.get('exit_code')} timed_out={res.get('timed_out')} truncated={res.get('truncated')}"
                     + (f" running run_id={res.get('run_id')}" if res.get("status") == "running" else ""))
        _thread_log(a.get("thread_id"), f"{a['tool']} was approved by the user and ran (approval {aid})")
        memory.act_record(a.get("thread_id"), a["tool"], a["payload_hash"], aid)
        _route(res, a.get("thread_id"))
        return {"status": "done", "result": res}
    except Exception as e:
        db.x("UPDATE approvals SET status='failed', result=? WHERE id=?", repr(e)[:2000], aid)
        db.audit(decided_by, a["tool"], a["payload_hash"], "approved_error", aid)
        _thread_log(a.get("thread_id"), f"{a['tool']} was approved but failed (approval {aid})")
        return {"status": "failed", "error": repr(e)}


def _approved_caller(a: dict, t) -> Caller | None:
    """Who a state write runs as once the owner approved it. A write proposed by a run that had read untrusted content
    runs as that kind of caller (tainted, limited to read scope), so what it stores is labelled untrusted, a job it
    creates can't act, and a watch it starts is read-only. Everything else runs as before (no caller)."""
    if not (a.get("tainted") and getattr(t, "writes_state", False)):
        return None
    return Caller(a.get("requested_by") or "approval", "remote", {"sources", "jobs"}, untrusted=True, approved_write=True,
                  thread_id=a.get("thread_id"))


def approval_status(aid: str) -> dict:
    rows = db.q("SELECT id,tool,status,reason,created,expires,decided,result FROM approvals WHERE id=?", aid)
    return rows[0] if rows else {"status": "unknown"}


# --- waiting for a decision (an MCP client's call returns the outcome instead of ending its turn) -----------
SETTLED = {"done", "denied", "failed", "expired", "tampered"}
WAITER_LIVE_SECONDS = 6.0  # a waiter counts as alive this long after its last heartbeat (any process)
_waiters: dict[str, set[asyncio.Event]] = {}


def _resolve(aid: str) -> None:
    for ev in list(_waiters.get(aid, ())):
        ev.set()


def _expire(aid: str, a: dict) -> None:
    if db.x("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", aid):
        _thread_log(a.get("thread_id"), f"approval for {a['tool']} expired")
    _resolve(aid)


def _outcome(row: dict) -> dict:
    """approval_status-shaped dict for a settled approval: status plus parsed result (done) or error (failed)."""
    out = {"status": row["status"], "tool": row["tool"], "approval_id": row["id"]}
    raw = row.get("result")
    if row["status"] == "done":
        try:
            out["result"] = json.loads(raw) if raw is not None else None
        except ValueError:
            out["result"] = raw
    elif row["status"] == "failed":
        out["error"] = raw
    return out


def _settled(aid: str) -> dict | None:
    rows = db.q("SELECT * FROM approvals WHERE id=?", aid)
    if not rows:
        return {"status": "unknown", "approval_id": aid}
    a = rows[0]
    if a["status"] == "pending" and time.time() > a["expires"]:
        _expire(aid, a)
        a = db.q("SELECT * FROM approvals WHERE id=?", aid)[0]
    return _outcome(a) if a["status"] in SETTLED else None


def _heartbeat(aid: str, on: bool = True) -> None:
    db.x("UPDATE approvals SET waiter_seen=? WHERE id=?", time.time() if on else 0, aid)


def has_live_waiter(aid: str) -> bool:
    r = db.q("SELECT waiter_seen FROM approvals WHERE id=?", aid)
    return bool(r and r[0]["waiter_seen"] and time.time() - r[0]["waiter_seen"] < WAITER_LIVE_SECONDS)


def mark_delivered(aid: str) -> None:
    """The requesting client got the outcome directly; continue_after_approval then only writes the thread note."""
    db.x("UPDATE approvals SET delivered_to_requester=1 WHERE id=?", aid)


def delivered(aid: str) -> bool:
    r = db.q("SELECT delivered_to_requester FROM approvals WHERE id=?", aid)
    return bool(r and r[0]["delivered_to_requester"])


async def wait_for_decision(aid: str, timeout: float, deliver: bool = False) -> dict:
    """Wait up to `timeout` seconds for the owner's decision. Returns the settled outcome (status done, denied,
    failed, expired, tampered, or unknown) or {"status": "pending", ...} when the time ran out. Wakes at once for a
    decision made in this process and polls the database for one made in another (stdio MCP vs HTTP server).
    Cancelling the task stops the wait. With deliver=True the approval is marked delivered_to_requester as the
    outcome is returned (before the waiter heartbeat is dropped, so continue_after_approval never sees a gap)."""
    poll = max(0.01, float(config.load().approval_poll_seconds))
    ev = asyncio.Event()
    _waiters.setdefault(aid, set()).add(ev)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            _heartbeat(aid)
            done = _settled(aid)
            if done is not None:
                if deliver and done["status"] != "unknown":
                    mark_delivered(aid)
                return done
            left = deadline - time.monotonic()
            if left <= 0:
                return {"status": "pending", "approval_id": aid}
            try:
                await asyncio.wait_for(ev.wait(), min(poll, left))
            except asyncio.TimeoutError:
                pass
            ev.clear()
    finally:
        _waiters.get(aid, set()).discard(ev)
        if not _waiters.get(aid):
            _waiters.pop(aid, None)
        with contextlib.suppress(Exception):
            _heartbeat(aid, False)


def pending_payload(res: dict) -> dict:
    """A still-pending payload for a client, with the instruction to keep waiting instead of asking the user."""
    aid = res.get("approval_id")
    return {**res, "next": f'Call pmjc_approval_wait(approval_id="{aid}") to keep waiting for the owner\'s decision; '
                           "do not ask the user to prompt you again."}


def may_see(caller: Caller, aid: str) -> bool:
    """Only the caller that requested an approval, or an admin, may see or wait on it."""
    r = db.q("SELECT requested_by FROM approvals WHERE id=?", aid)
    return bool(r) and (caller.can("admin") or r[0]["requested_by"] == caller.name)
