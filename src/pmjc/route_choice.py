"""GPU busy: ask the user instead of silently routing to the cloud (phase4/ac, decided 2026-11-09).

When the plan's first choice is a guarded local provider (`extra.skip_when_gpu_busy_percent`) and the GPU is busy, an
interactive request does not run. It returns a notice with the choices:
  - use local anyway: an approval of the internal `pmjc_route_override` (it competes with whatever the owner is doing),
  - use a cloud model: the same kind of row, offered only if the thread isn't private and a cloud candidate exists,
  - wait: nothing to do, ask again later.
Deciding a row stores a one-shot override for the thread; the original request is then run again in the same thread
(Discord: as the next turn; REST, CLI and links: through ask.continue_after_approval). The tool is internal: it is not
registered, so no model can call it.
"""
from __future__ import annotations

import json
import secrets
import time

from . import auth, config, db, router

TOOL = "pmjc_route_override"
DDL = "CREATE TABLE IF NOT EXISTS route_override (thread_id TEXT PRIMARY KEY, mode TEXT, ts REAL)"
MODES = ("local", "cloud")


def _init() -> None:
    db.x(DDL)


def gate(plan, private: bool) -> dict | None:
    """GPU facts when this interactive plan wants a guarded local provider and the GPU is busy now, else None."""
    if not plan.interactive or plan.override or not plan.order:
        return None
    p = next((x for x in config.load().routing if x.id == plan.order[0]), None)
    if p is None or p.trust != "local" or not p.extra.get("skip_when_gpu_busy_percent"):
        return None
    info = router.gpu_info(int(p.extra["skip_when_gpu_busy_percent"]))
    if not info["busy"]:
        return None
    return {**info, "provider": p.id}


def action_for(private: bool) -> str:
    a = config.load().router.gpu_busy_action
    a = a if a in ("ask", "cloud", "local") else "ask"
    return "ask" if (a == "cloud" and private) else a  # private work never goes to the cloud


def gpu_text(info: dict) -> str:
    bits = list(info.get("processes") or [])
    if info.get("util") is not None:
        bits.append(f"{info['util']}% utilization")
    return ", ".join(bits) or "in use"


def cloud_ids(plan) -> list[str]:
    local = {p.id for p in config.load().routing if p.trust == "local"}
    return [c for c in plan.candidates if c not in local]


def link(aid: str) -> str:
    return f"{config.load().public_url.rstrip('/')}/approve/{aid}?sig={auth.sign('approve:' + aid)}"


def offer(caller, thread_id: str, text: str, info: dict, private: bool, plan) -> dict:
    """Create the choice rows and return {answer, route_choice} for the door to show."""
    from .executor import payload_hash
    group = secrets.token_urlsafe(6)
    ttl = config.load().approval_ttl_seconds
    now = time.time()
    opts = []
    modes = ["local"] + (["cloud"] if not private and cloud_ids(plan) else [])
    labels = {"local": "Use local anyway (it competes with what is using the GPU; needs your approval)",
              "cloud": "Use a cloud model (re-runs this request on the strong tier)"}
    for mode in modes:
        args = {"mode": mode, "request": text, "group": group}
        aid = secrets.token_urlsafe(9)
        db.x("INSERT INTO approvals(id,tool,args,payload_hash,requested_by,reason,status,created,expires,thread_id,tainted) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?)", aid, TOOL, json.dumps(args), payload_hash(TOOL, args), caller.name,
             f"GPU busy ({gpu_text(info)}): run this request on {mode}", "pending", now, now + ttl, thread_id, 0)
        opts.append({"id": mode, "label": labels[mode], "approval_id": aid, "approve_url": link(aid)})
    opts.append({"id": "wait", "label": "Wait: do nothing, ask again later"})
    lines = [f"The GPU is busy ({gpu_text(info)}), so I haven't started the local model {info['provider']}. Choose:"]
    lines += [f"{i}. {o['label']}" for i, o in enumerate(opts, 1)]
    return {"answer": "\n".join(lines), "route_choice": {"reason": "gpu_busy", "gpu": gpu_text(info), "options": opts,
                                                          "thread_id": thread_id}}


def set_override(thread_id: str, mode: str) -> None:
    _init()
    db.x("INSERT INTO route_override(thread_id,mode,ts) VALUES(?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
         "mode=excluded.mode, ts=excluded.ts", thread_id, mode, time.time())


def take(thread_id: str | None) -> str | None:
    """The one-shot choice recorded for this thread (consumed), if it is still fresh."""
    if not thread_id:
        return None
    _init()
    r = db.q("SELECT mode, ts FROM route_override WHERE thread_id=?", thread_id)
    if not r:
        return None
    db.x("DELETE FROM route_override WHERE thread_id=?", thread_id)
    return r[0]["mode"] if r[0]["mode"] in MODES and time.time() - r[0]["ts"] < 3600 else None


def decided(a: dict, args: dict) -> dict:
    """The owner approved a choice row: store the override, withdraw the sibling choices."""
    mode = args.get("mode")
    if mode not in MODES or not a.get("thread_id"):
        raise ValueError("bad route choice")
    set_override(a["thread_id"], mode)
    for sib in db.q("SELECT id, args FROM approvals WHERE tool=? AND status='pending' AND thread_id=?", TOOL, a["thread_id"]):
        if json.loads(sib["args"]).get("group") == args.get("group"):
            db.x("UPDATE approvals SET status='denied' WHERE id=? AND status='pending'", sib["id"])
    return {"route": mode}
