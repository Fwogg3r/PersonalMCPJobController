"""OpenAI-compatible front door: any chat client that speaks /v1/chat/completions talks to the pmjc assistant.

`GET /v1/models` lists virtual models (pmjc, pmjc-local, pmjc:<provider id>); `POST /v1/chat/completions` runs
`ask()` on the last user message in the conversation's pmjc thread. Auth is the usual bearer token (full owner access).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import json
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent, config, db, memory
from .auth import Caller

DDL = "CREATE TABLE IF NOT EXISTS door_threads (key TEXT PRIMARY KEY, thread_id TEXT, ts REAL)"
_hits: dict[str, deque] = defaultdict(deque)


def _err(status: int, msg: str, kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": kind, "code": None}}, status_code=status)


def model_list() -> list[str]:
    ids = ["pmjc", "pmjc-local"]
    ids += [f"pmjc:{p.id}" for p in config.load().routing if p.enabled]
    return ids


def resolve_model(model: str) -> tuple[bool, list[str] | None] | None:
    """(private, provider_order) for a virtual model id, or None if unknown."""
    if model in ("", "pmjc"):
        return False, None
    if model == "pmjc-local":
        return True, None
    if model.startswith("pmjc:"):
        pid = model[5:]
        ids = [p.id for p in config.load().routing if p.enabled]
        if pid not in ids:
            return None
        return False, [pid] + [i for i in ids if i != pid]
    return None


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _norm(s: str) -> str:
    """Assistant text as the client will echo it back: progress lines dropped, whitespace collapsed, first 500 chars."""
    lines = [ln for ln in s.splitlines() if not re.fullmatch(r"\s*_calling .*\.\.\._\s*", ln)]
    return " ".join(" ".join(lines).split())[:500]


def pair_key(caller_name: str, first_user: str, first_answer: str) -> str:
    return hashlib.sha256(f"pair\0{caller_name}\0{first_user.strip()}\0{_norm(first_answer)}".encode()).hexdigest()


def conversation_key(caller_name: str, body: dict, headers, messages: list) -> str | None:
    """Stable key for the conversation, always per token. A client-provided id wins. Otherwise the first user
    message together with the first assistant reply identifies it (two chats that both open with "hi" get
    different answers, so different threads). None means a brand-new conversation: no reply yet, so a new thread."""
    cid = headers.get("x-conversation-id") or body.get("conversation_id")
    meta = body.get("metadata")
    if not cid and isinstance(meta, dict):
        cid = meta.get("conversation_id") or meta.get("chat_id")
    if not cid and isinstance(body.get("user"), str):
        cid = body["user"]
    if cid:
        return hashlib.sha256(f"id\0{caller_name}\0{cid}".encode()).hexdigest()
    first = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
    reply = next((text_of(m.get("content")) for m in messages if m.get("role") == "assistant"), None)
    if reply is None:
        return None
    return pair_key(caller_name, first, reply)


def thread_for(key: str) -> str | None:
    db.x(DDL)
    rows = db.q("SELECT thread_id FROM door_threads WHERE key=?", key)
    if rows and memory.thread_exists(rows[0]["thread_id"]):
        return rows[0]["thread_id"]
    return None


def thread_store(key: str, tid: str) -> None:
    db.x(DDL)
    db.x("INSERT OR REPLACE INTO door_threads(key,thread_id,ts) VALUES(?,?,?)", key, tid, time.time())


def approvals_block(thread_id: str, since: float) -> str:
    from . import auth
    base = config.load().public_url.rstrip("/")
    out = []
    for a in db.q("SELECT id, tool, reason FROM approvals WHERE thread_id=? AND status='pending' AND created>=?",
                  thread_id, since - 1):
        aid = a["id"]
        ok = f"{base}/approve/{aid}?sig={auth.sign('approve:' + aid)}"
        no = f"{base}/approve/{aid}?deny=1&sig={auth.sign('deny:' + aid)}"
        why = f" ({a['reason'][:200]})" if a.get("reason") else ""
        out.append(f"- `{a['tool']}`{why}\n  [Approve]({ok}) / [Deny]({no})")
    if not out:
        return ""
    return "\n\n---\n**Waiting for your approval:**\n" + "\n".join(out)


def _rate_ok(name: str, limit: int) -> bool:
    now = time.time()
    q = _hits[name]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None = None) -> str:
    d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
         "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(d)}\n\n"


def _errtext(e: Exception) -> tuple[int, str]:
    if isinstance(e, agent.NoProvider):
        return 503, str(e)
    if isinstance(e, ValueError):
        return 400, str(e)
    return 500, f"{type(e).__name__}: {e}"[:500]


def register(app: FastAPI, bearer, need) -> None:
    @app.get("/v1/models", tags=["openai"])
    async def models(_: Caller = Depends(need("sources"))):
        if config.load().front_door.enabled is False:
            return _err(404, "front door disabled")
        now = int(time.time())
        return {"object": "list", "data": [{"id": m, "object": "model", "created": now, "owned_by": "pmjc"}
                                           for m in model_list()]}

    @app.post("/v1/chat/completions", tags=["openai"])
    async def completions(request: Request, c: Caller = Depends(need("sources"))):
        fd = config.load().front_door
        if fd.enabled is False:
            return _err(404, "front door disabled")
        raw = await request.body()
        if len(raw) > fd.max_body_bytes:
            return _err(413, "request too large")
        if not _rate_ok(c.name, fd.rate_limit_per_minute):
            return _err(429, "rate limit exceeded", "rate_limit_error")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return _err(400, "invalid JSON body")
        messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                text = text_of(m.get("content")).strip()
                break
        if not text:
            return _err(400, "no user message")
        model = body.get("model") or "pmjc"
        mode = resolve_model(model)
        if mode is None:
            return _err(404, f"unknown model {model!r}; GET /v1/models lists them", "model_not_found")
        private, order = mode
        key = conversation_key(c.name, body, request.headers, messages)
        tid = thread_for(key) if key else None
        first_user = next((text_of(m.get("content")) for m in messages if m.get("role") == "user"), "")
        caller = Caller(f"ask:{c.name}", "remote", set(c.scopes))
        queue: asyncio.Queue = asyncio.Queue()
        stream = bool(body.get("stream"))
        if stream and fd.stream_progress:
            async def on_tool(name, args, status):
                await queue.put(f"_calling {name}..._\n\n")
            caller.on_tool = on_tool
        started = time.time()

        async def work() -> dict:
            from . import ask as ask_mod
            res = await ask_mod.ask(caller, text, private=private, provider_order=order, thread_id=tid,
                                    history_turns=fd.history_turns)
            answer = res["answer"] + approvals_block(res["thread_id"], started)
            thread_store(key or pair_key(c.name, first_user, answer), res["thread_id"])
            usage = res.get("usage") or {}
            pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            return {"answer": answer, "thread_id": res["thread_id"],
                    "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}

        cid, created = "chatcmpl-" + secrets.token_hex(8), int(time.time())

        if not stream:
            try:
                r = await work()
            except Exception as e:
                code, msg = _errtext(e)
                return _err(code, msg, "server_error")
            return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model,
                                 "pmjc_thread_id": r["thread_id"],
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": r["answer"]},
                                              "finish_reason": "stop"}], "usage": r["usage"]})

        async def gen():
            yield _chunk(cid, created, model, {"role": "assistant", "content": ""})
            task = asyncio.ensure_future(work())
            try:
                while not task.done():
                    getter = asyncio.ensure_future(queue.get())
                    done, _ = await asyncio.wait({task, getter}, timeout=fd.keepalive_seconds,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield _chunk(cid, created, model, {"content": getter.result()})
                    else:
                        getter.cancel()
                        if not done:
                            yield ": keep-alive\n\n"
                while not queue.empty():
                    yield _chunk(cid, created, model, {"content": queue.get_nowait()})
                try:
                    content = task.result()["answer"]
                except Exception as e:
                    content = f"pmjc error: {_errtext(e)[1]}"
                yield _chunk(cid, created, model, {"content": content})
                yield _chunk(cid, created, model, {}, "stop")
                yield "data: [DONE]\n\n"
            finally:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
