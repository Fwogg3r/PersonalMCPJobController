"""Streaming model calls for openai_compat providers, with progress while the prompt is read and a per-call log.

`call()` POSTs one chat completion with stream=true and rebuilds the same message a non-streaming call returns
(content, reasoning, tool_calls), so the agent loop doesn't care which it got. While it streams:
- before the first token (the model is reading the prompt), the caller's `on_notice` gets a line every
  `router.local_start_notice_seconds`: elapsed time, prompt size, and a percent estimated from this model's past
  prompt-reading speed (the `completions` table) or, if the provider sets `extra.progress_log`, read from LM Studio's
  server log ("Prompt processing progress: 37.8%");
- the caller's `on_delta(kind, text)` (only /v1 streaming sets it) gets the answer ("content") and the model's
  thinking ("reasoning") as they arrive. Forwarding stops once the run has read untrusted content: that answer goes
  out whole after the link check (pmjc.defang). Raw `<tool_call>` text that a server failed to parse is not
  forwarded; `<think>...</think>` inside content is forwarded as reasoning (tags may be split across chunks).

Every call, failed and cancelled ones included, writes one row to the `completions` table (metadata only, never
message text) and one INFO log line. `pmjc completions` shows them (decided 2026-11-09, docs/DECISIONS.md).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from . import config, db
from .config import ProviderConfig

log = logging.getLogger(__name__)

@dataclass
class Call:
    """What one model call returned plus its timings."""
    message: dict = field(default_factory=dict)  # role, content, tool_calls (as a non-streaming response has them)
    usage: dict = field(default_factory=dict)
    finish: str = ""
    ttft_s: float | None = None  # seconds to the first token (≈ prompt reading)
    total_s: float = 0.0
    forwarded_all: bool = False  # all of this turn's content went out through on_delta


class _ThinkSplit:
    """Splits streamed content into (kind, text) pieces, routing <think>...</think> to reasoning. Holds back a
    possible partial tag at the end of a chunk until the next one arrives."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                if i:
                    out.append(("reasoning" if self.inside else "content", self.buf[:i]))
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            keep = 0  # longest suffix that could start the tag
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if tag.startswith(self.buf[-k:]):
                    keep = k
                    break
            emit = self.buf[:len(self.buf) - keep]
            if emit:
                out.append(("reasoning" if self.inside else "content", emit))
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        return [("reasoning" if self.inside else "content", rest)] if rest else []


def _merge_tool_calls(acc: dict[int, dict], deltas: list) -> None:
    """Rebuild tool calls from streamed fragments, keyed by index (id and name come only in the first fragment)."""
    for d in deltas or []:
        idx = d.get("index")
        idx = 0 if idx is None else int(idx)
        tc = acc.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        if d.get("id"):
            tc["id"] = d["id"]
        fn = d.get("function") or {}
        if fn.get("name"):
            tc["function"]["name"] = fn["name"]
        frag = fn.get("arguments")
        if frag:
            cur = tc["function"]["arguments"]
            if cur and frag == cur and _is_json(cur):
                continue  # a server that repeats the complete arguments
            tc["function"]["arguments"] = cur + frag


def _is_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


def prefill_rate(p: ProviderConfig) -> float | None:
    """Median past prompt-reading speed (tokens/s) of this provider and model, from calls that took over 2 s."""
    rows = db.q("SELECT prompt_tokens, ttft_s FROM completions WHERE provider=? AND model=? AND status='ok' "
                "AND ttft_s > 2 AND prompt_tokens > 0 ORDER BY id DESC LIMIT 20", p.id, p.model or "")
    rates = sorted(r["prompt_tokens"] / r["ttft_s"] for r in rows)
    return rates[len(rates) // 2] if rates else None


class _LogTail:
    """Reads LM Studio's newest server log from the call's start for "Prompt processing progress: N%" lines.
    Only an estimate: another client of the same LM Studio shows up here too."""
    RX = re.compile(r"Prompt processing progress: ([\d.]+)%")

    def __init__(self, folder: str):
        self.path, self.pos = None, 0
        try:
            files = glob.glob(os.path.join(os.path.expanduser(folder), "**", "*.log"), recursive=True)
            if files:
                self.path = max(files, key=os.path.getmtime)
                self.pos = os.path.getsize(self.path)
        except OSError:
            self.path = None

    def percent(self) -> float | None:
        if not self.path:
            return None
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read().decode("utf-8", "replace")
        except OSError:
            return None
        hits = self.RX.findall(chunk)
        return float(hits[-1]) if hits else None


def _fmt_k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


async def call(c: httpx.AsyncClient, p: ProviderConfig, body: dict, headers: dict, caller, *, prompt_tokens_est: int,
               turn: int = 0, tools_sent: int = 0, schema_tokens: int = 0) -> Call:
    """One streamed chat completion. Raises like the non-streaming call did (RuntimeError with the server's reason)."""
    out = Call()
    t0 = time.monotonic()
    status, err = "ok", ""
    first = asyncio.Event()
    notify = getattr(caller, "on_notice", None)
    every = config.load().router.local_start_notice_seconds
    beat = asyncio.create_task(_heartbeat(p, notify, first, t0, prompt_tokens_est, every)) if notify and every > 0 else None
    try:
        await _stream(c, p, body, headers, caller, out, first, t0)
    except asyncio.CancelledError:
        status, err = "cancelled", "client went away"
        raise
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {e}"[:300]
        raise
    finally:
        if beat:
            beat.cancel()
        out.total_s = time.monotonic() - t0
        _record(p, caller, out, turn, tools_sent, schema_tokens, status, err)
    return out


async def _heartbeat(p, notify, first: asyncio.Event, t0: float, est: int, every: float) -> None:
    tail = _LogTail(p.extra["progress_log"]) if p.extra.get("progress_log") else None
    rate = prefill_rate(p)
    while True:
        try:
            await asyncio.wait_for(first.wait(), every)
            return
        except asyncio.TimeoutError:
            pass
        el = time.monotonic() - t0
        pct = tail.percent() if tail else None
        if pct is not None:
            how = f", LM Studio {pct:.0f}%"
        elif rate:
            how = f", ~{min(99, el * rate / max(est, 1) * 100):.0f}% by past speed"
        else:
            how = ""
        try:
            await notify(f"_model reading the prompt (~{_fmt_k(est)} tokens{how})… {el:.0f}s_")
        except Exception:
            log.debug("notice failed", exc_info=True)


async def _stream(c, p, body, headers, caller, out: Call, first: asyncio.Event, t0: float) -> None:
    body = dict(body, stream=True, stream_options={"include_usage": True})
    on_delta = getattr(caller, "on_delta", None)
    split = _ThinkSplit()
    content, reasoning, calls = [], [], {}
    forwarding = on_delta is not None
    blocked = False  # raw <tool_call> text seen: stop forwarding this turn's content

    async def fwd(kind, text):
        nonlocal forwarding
        if not forwarding or not text:
            return
        if getattr(caller, "untrusted", False):
            forwarding = False  # untrusted content read: the answer goes out whole after the link check
            return
        try:
            await on_delta(kind, text)
        except Exception:
            log.debug("delta failed", exc_info=True)

    url = p.base_url.rstrip("/") + "/chat/completions"
    async with c.stream("POST", url, json=body, headers=headers) as r:
        if r.status_code >= 400:
            text = (await r.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {r.status_code} from {p.id}: {text[:400]}")
        if "event-stream" not in r.headers.get("content-type", ""):  # the server ignored stream=true: plain JSON
            data = json.loads(await r.aread())
            out.ttft_s = time.monotonic() - t0
            first.set()
            ch = (data.get("choices") or [{}])[0]
            out.message, out.usage, out.finish = ch.get("message") or {}, data.get("usage") or {}, ch.get("finish_reason") or ""
            return
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if ev.get("error"):
                raise RuntimeError(f"{p.id}: {str(ev['error'])[:400]}")
            if ev.get("usage"):
                out.usage = ev["usage"]
            for ch in ev.get("choices") or []:  # the usage chunk has choices: []
                d = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    out.finish = ch["finish_reason"]
                r_txt = d.get("reasoning_content") or d.get("reasoning") or ""
                c_txt = d.get("content") or ""
                if (r_txt or c_txt or d.get("tool_calls")) and not first.is_set():
                    out.ttft_s = time.monotonic() - t0
                    first.set()
                if r_txt:
                    reasoning.append(r_txt)
                    await fwd("reasoning", r_txt)
                if d.get("tool_calls"):
                    _merge_tool_calls(calls, d["tool_calls"])
                if c_txt:
                    for kind, piece in split.feed(c_txt):
                        await _piece(kind, piece, content, reasoning, fwd, lambda: blocked)
                        if kind == "content" and "<tool_call" in "".join(content)[-200:]:
                            blocked = True
    for kind, piece in split.flush():
        await _piece(kind, piece, content, reasoning, fwd, lambda: blocked)
    text = "".join(content)
    out.message = {"role": "assistant", "content": text}
    if reasoning:
        out.message["reasoning_content"] = "".join(reasoning)
    if calls:
        out.message["tool_calls"] = [calls[i] for i in sorted(calls)]
    out.forwarded_all = forwarding and not blocked and on_delta is not None


async def _piece(kind, piece, content, reasoning, fwd, blocked) -> None:
    if kind == "reasoning":
        reasoning.append(piece)
        await fwd("reasoning", piece)
        return
    before = "".join(content)
    content.append(piece)
    if blocked() or "<tool_call" in (before[-20:] + piece):
        return
    await fwd("content", piece)


def _record(p: ProviderConfig, caller, out: Call, turn, tools_sent, schema_tokens, status, err) -> None:
    u = out.usage or {}
    pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    rt = int(((u.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    gen_s = out.total_s - (out.ttft_s or 0)
    tps = round(ct / gen_s, 1) if ct and gen_s > 0.05 else None
    try:
            db.x("INSERT INTO completions(ts,caller,thread_id,provider,model,turn,tools_sent,schema_tokens,prompt_tokens,"
             "cached_tokens,completion_tokens,reasoning_tokens,ttft_s,total_s,gen_tps,finish,status,error,effort) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             time.time(), getattr(caller, "name", ""), getattr(caller, "thread_id", None), p.id, p.model or "", turn,
             tools_sent, schema_tokens, pt, cached, ct, rt,
             round(out.ttft_s, 2) if out.ttft_s is not None else None, round(out.total_s, 2), tps, out.finish,
             status, err, getattr(getattr(caller, "effort", None), "level", None))
    except Exception:
        log.debug("completion log write failed", exc_info=True)
    log.info("completion %s %s turn %s: %s prompt tok (%s cached), %s tools, first token %s, %s out tok%s, %.1fs total%s",
             p.id, p.model or "", turn, pt, cached, tools_sent,
             f"{out.ttft_s:.1f}s" if out.ttft_s is not None else "-", ct, f" at {tps} tok/s" if tps else "",
             out.total_s, f" [{status}: {err}]" if status != "ok" else "")


def prune(retention_days: int) -> None:
    db.x("DELETE FROM completions WHERE ts < ?", time.time() - retention_days * 86400)


def recent(n: int = 20, provider: str | None = None) -> list[dict]:
    if provider:
        return db.q("SELECT * FROM completions WHERE provider=? ORDER BY id DESC LIMIT ?", provider, n)
    return db.q("SELECT * FROM completions ORDER BY id DESC LIMIT ?", n)
