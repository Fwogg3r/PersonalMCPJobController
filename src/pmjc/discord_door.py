"""Discord front door: chat with pmjc from any device through your own bot.

Off by default (settings.discord_door). The service opens an outbound websocket to the Discord gateway, so no
inbound port or tunnel is needed. Only messages from `owner_user_ids` are read; everyone else is ignored.

- A message in one of `channel_ids` starts a Discord thread and a pmjc thread. Replies in that Discord thread
  continue the conversation; the last `history_turns` exchanges go back to the model.
- A DM to the bot works the same way, one conversation per DM until you send `new`.
- Each message runs through ask() (connectors, watches, jobs) with the configured scopes. Act tools still need
  approval: the door posts Approve/Deny buttons in the conversation, and only an owner's click counts.
- With the door running, Discord notifications for other approvals use the same buttons instead of links.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time

from . import agent, config, db, executor, registry
from .auth import Caller

log = logging.getLogger("pmjc.discord_door")

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS = (1 << 9) | (1 << 12) | (1 << 15)  # guild messages, direct messages, message content
FATAL_CLOSE = {4004: "invalid bot token", 4010: "invalid shard", 4011: "sharding required", 4012: "bad API version",
               4013: "invalid intents", 4014: "Message Content intent not enabled in the Developer Portal"}
NEW_WORDS = {"new", "/new", "!new", "reset"}
MAX_MSG = 2000
DDL = "CREATE TABLE IF NOT EXISTS discord_door (channel_id TEXT PRIMARY KEY, thread_id TEXT, created REAL)"
BUTTON_PREFIX = "pmjc:"

_door: Door | None = None


def running() -> Door | None:
    """The live door, if the service started one (used by Discord.notify to choose buttons over links)."""
    return _door if _door and _door.connected else None


def chunks(text: str, size: int = MAX_MSG) -> list[str]:
    """Split on line breaks where possible; Discord rejects messages over 2000 characters."""
    text = text.strip() or "(no answer)"
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + [text]


def approval_buttons(aid: str) -> list[dict]:
    return [{"type": 1, "components": [
        {"type": 2, "style": 3, "label": "Approve", "custom_id": f"{BUTTON_PREFIX}a:{aid}"},
        {"type": 2, "style": 4, "label": "Deny", "custom_id": f"{BUTTON_PREFIX}d:{aid}"}]}]


def approval_text(a: dict) -> str:
    args = a.get("args") or "{}"
    if len(args) > 900:
        args = args[:900] + "..."
    reason = f"\n{a['reason']}" if a.get("reason") else ""
    return f"**Approval needed:** `{a['tool']}`{reason}\n```json\n{args}\n```"


class Door:
    """Message and button handling. Transport-independent: `api(method, path, json)` does the REST calls."""

    def __init__(self, cfg: config.DiscordDoorConfig, api, bot_id: str = ""):
        self.cfg = cfg
        self.api = api
        self.bot_id = bot_id
        self.connected = False
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        db.x(DDL)

    # ---- conversation map
    def thread_for(self, channel_id: str) -> str | None:
        r = db.q("SELECT thread_id FROM discord_door WHERE channel_id=?", channel_id)
        return r[0]["thread_id"] if r else None

    def bind(self, channel_id: str, thread_id: str) -> None:
        db.x("INSERT OR REPLACE INTO discord_door(channel_id, thread_id, created) VALUES(?,?,?)",
             channel_id, thread_id, time.time())

    def unbind(self, channel_id: str) -> None:
        db.x("DELETE FROM discord_door WHERE channel_id=?", channel_id)

    def is_owner(self, user_id: str | None) -> bool:
        return bool(user_id) and str(user_id) in {str(u) for u in self.cfg.owner_user_ids}

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---- dispatch events
    async def on_message(self, m: dict) -> None:
        author = m.get("author") or {}
        if author.get("bot") or not self.is_owner(author.get("id")):
            return  # never read anyone else's messages
        text = (m.get("content") or "").strip()
        attachments = [a.get("url") for a in m.get("attachments", []) if a.get("url")]
        if attachments:
            text += "\n(attachments: " + ", ".join(attachments) + ")"
        if not text:
            return
        ch = m["channel_id"]
        is_dm = not m.get("guild_id")
        if self.thread_for(ch) is None and not is_dm and ch not in self.cfg.channel_ids:
            return  # a channel or thread the door doesn't own
        lock = self._locks.setdefault(ch, asyncio.Lock())
        async with lock:  # one run at a time per conversation, in order
            await self._handle(m, ch, is_dm, text, author["id"])

    async def _handle(self, m: dict, ch: str, is_dm: bool, text: str, user_id: str) -> None:
        tid = self.thread_for(ch)
        reply_ch = ch
        if is_dm and text.lower() in NEW_WORDS:
            self.unbind(ch)
            await self.send(ch, "Started a new conversation.")
            return
        if tid is None and not is_dm:  # a new message in a front-door channel: give it its own Discord thread
            name = " ".join(text.split())[:90] or "pmjc"
            t = await self.api("POST", f"/channels/{ch}/messages/{m['id']}/threads",
                               {"name": name, "auto_archive_duration": 1440})
            reply_ch = t["id"]
        caller = Caller(f"discord:{user_id}", "remote", set(self.cfg.scopes))
        started = time.time()
        typing = asyncio.create_task(self._typing(reply_ch))
        try:
            from . import ask as ask_mod
            res = await ask_mod.ask(caller, text, private=self.cfg.private, thread_id=tid,
                                    history_turns=self.cfg.history_turns)
        except agent.NoProvider as e:
            await self.send(reply_ch, f"No model is available right now: {e}")
            return
        except Exception as e:
            log.exception("discord door run failed")
            await self.send(reply_ch, f"That failed: {type(e).__name__}: {str(e)[:300]}")
            return
        finally:
            typing.cancel()
        if tid is None:
            self.bind(reply_ch, res["thread_id"])
        footer = f"\n-# {res['provider']}"
        if res.get("watches"):
            footer += f" · watching ({', '.join(res['watches'])})"
        if res.get("cost_usd"):
            footer += f" · ${res['cost_usd']:.4f}"
        for part in chunks(res["answer"] + footer):
            await self.send(reply_ch, part)
        for a in db.q("SELECT id, tool, args, reason FROM approvals WHERE thread_id=? AND status='pending' AND created>=?",
                      res["thread_id"], started - 1):
            await self.send(reply_ch, approval_text(a), components=approval_buttons(a["id"]))

    async def on_interaction(self, i: dict) -> None:
        if i.get("type") != 3:  # message component (button)
            return
        cid = (i.get("data") or {}).get("custom_id", "")
        if not cid.startswith(BUTTON_PREFIX):
            return
        user = (i.get("member") or {}).get("user") or i.get("user") or {}
        cb = f"/interactions/{i['id']}/{i['token']}/callback"
        if not self.is_owner(user.get("id")):
            await self.api("POST", cb, {"type": 4, "data": {"content": "Only the owner can decide this.", "flags": 64}})
            return
        kind, _, aid = cid[len(BUTTON_PREFIX):].partition(":")
        await self.api("POST", cb, {"type": 6})  # acknowledge within 3 s; the tool may take longer
        res = await executor.decide(aid, kind == "a", f"discord:{user['id']}")
        status = res.get("status", "?")
        detail = ""
        if "result" in res and res["result"] is not None:
            detail = json.dumps(res["result"], default=str) if not isinstance(res["result"], str) else res["result"]
        elif res.get("error"):
            detail = res["error"]
        body = ((i.get("message") or {}).get("content") or "")[:1200]
        body += f"\n**{status}** by <@{user['id']}>" + (f"\n```\n{detail[:600]}\n```" if detail else "")
        app_id = i.get("application_id") or self.bot_id
        await self.api("PATCH", f"/webhooks/{app_id}/{i['token']}/messages/@original",
                       {"content": body[:MAX_MSG], "components": [], "allowed_mentions": {"parse": []}})

    # ---- REST helpers
    async def send(self, channel_id: str, content: str, components: list | None = None) -> dict:
        body = {"content": content[:MAX_MSG], "allowed_mentions": {"parse": []}}
        if components:
            body["components"] = components
        return await self.api("POST", f"/channels/{channel_id}/messages", body)

    async def _typing(self, channel_id: str) -> None:
        with contextlib.suppress(Exception):
            while True:
                await self.api("POST", f"/channels/{channel_id}/typing", None)
                await asyncio.sleep(8)


class Gateway:
    """Minimal Discord gateway client: identify, heartbeat, resume, reconnect with backoff."""

    def __init__(self, token: str, door: Door, url: str = GATEWAY):
        self.token = token
        self.url = url
        self.door = door
        self.seq: int | None = None
        self.session_id: str | None = None
        self.resume_url: str | None = None
        self.stopped = False
        self.fatal: str | None = None

    async def run(self) -> None:
        import websockets
        backoff = 2
        while not self.stopped:
            url = (self.resume_url or self.url.split("?")[0]) + "?v=10&encoding=json"
            try:
                async with websockets.connect(url, max_size=2 ** 22) as ws:
                    await self._session(ws)
                    backoff = 2
            except asyncio.CancelledError:
                raise
            except Exception as e:
                code = getattr(getattr(e, "rcvd", None), "code", None)
                if code in FATAL_CLOSE:
                    self.fatal = FATAL_CLOSE[code]
                    log.error("discord door stopped: %s (close %s)", self.fatal, code)
                    return
                if code in (4007, 4009):  # bad sequence / session timed out: start a new session
                    self.session_id = self.seq = self.resume_url = None
                log.warning("discord gateway dropped (%s); reconnecting in %ss", e or type(e).__name__, backoff)
            finally:
                self.door.connected = False
            if not self.stopped:
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 120)

    async def _session(self, ws) -> None:
        hello = json.loads(await ws.recv())
        interval = hello["d"]["heartbeat_interval"] / 1000
        acked = True

        async def beat():
            nonlocal acked
            await asyncio.sleep(interval * random.random())
            while True:
                if not acked:  # zombie connection: drop it and resume
                    await ws.close(4000)
                    return
                acked = False
                await ws.send(json.dumps({"op": 1, "d": self.seq}))
                await asyncio.sleep(interval)

        hb = asyncio.create_task(beat())
        try:
            if self.session_id:
                await ws.send(json.dumps({"op": 6, "d": {"token": self.token, "session_id": self.session_id, "seq": self.seq}}))
            else:
                await ws.send(json.dumps({"op": 2, "d": {"token": self.token, "intents": INTENTS,
                                                         "properties": {"os": "pmjc", "browser": "pmjc", "device": "pmjc"}}}))
            async for raw in ws:
                p = json.loads(raw)
                op = p.get("op")
                if p.get("s") is not None:
                    self.seq = p["s"]
                if op == 11:
                    acked = True
                elif op == 1:
                    await ws.send(json.dumps({"op": 1, "d": self.seq}))
                elif op == 7:  # server asks us to reconnect and resume
                    return
                elif op == 9:  # invalid session; d says whether it can be resumed
                    if not p.get("d"):
                        self.session_id = self.seq = self.resume_url = None
                    await asyncio.sleep(1 + 4 * random.random())
                    return
                elif op == 0:
                    self._dispatch(p.get("t"), p.get("d") or {})
        finally:
            hb.cancel()

    def _dispatch(self, t: str, d: dict) -> None:
        if t == "READY":
            self.session_id = d["session_id"]
            self.resume_url = d.get("resume_gateway_url")
            self.door.bot_id = d["user"]["id"]
            self.door.connected = True
            log.info("discord door connected as %s", d["user"].get("username"))
        elif t == "RESUMED":
            self.door.connected = True
        elif t == "MESSAGE_CREATE":
            self.door.spawn(self._guard(self.door.on_message(d)))
        elif t == "INTERACTION_CREATE":
            self.door.spawn(self._guard(self.door.on_interaction(d)))

    @staticmethod
    async def _guard(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("discord door event failed")


# ---- service hooks ---------------------------------------------------------------------------------------
_task: asyncio.Task | None = None
_gateway: Gateway | None = None


def problems() -> list[str]:
    """Why the door can't start (empty when it can)."""
    cfg = config.load().discord_door
    out = []
    if not cfg.enabled:
        out.append("disabled (settings.discord_door.enabled)")
    if not cfg.owner_user_ids:
        out.append("no owner_user_ids")
    con = registry.get().connectors.get("discord")
    if con is None or not con.configured():
        out.append("discord connector has no bot_token (pmjc auth set discord)")
    return out


def start() -> asyncio.Task | None:
    """Called from the server lifespan. Does nothing unless the door is enabled and configured."""
    global _door, _task, _gateway
    if problems():
        return None
    con = registry.get().connectors["discord"]
    _door = Door(config.load().discord_door, lambda method, path, body: con._api(method, path, json=body))
    _gateway = Gateway(con.secret("bot_token"), _door)
    _task = asyncio.create_task(_gateway.run())
    log.info("discord door starting")
    return _task


async def stop() -> None:
    global _door, _task, _gateway
    if _gateway:
        _gateway.stopped = True
    if _task:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await _task
    _door = _task = _gateway = None


def status() -> dict:
    cfg = config.load().discord_door
    return {"enabled": cfg.enabled, "connected": bool(_door and _door.connected),
            "error": _gateway.fatal if _gateway else None, "problems": problems(),
            "owners": len(cfg.owner_user_ids), "channels": cfg.channel_ids,
            "conversations": db.q("SELECT COUNT(*) n FROM discord_door")[0]["n"]
            if db.q("SELECT name FROM sqlite_master WHERE name='discord_door'") else 0}
