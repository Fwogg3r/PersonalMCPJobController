"""Discord front door: chat with pmjc from any device through your own bot.

Off by default (settings.discord_door). The service opens an outbound websocket to the Discord gateway, so no
inbound port or tunnel is needed. Only messages from `owner_user_ids` are read; everyone else is ignored,
except that their DMs to the bot (e.g. replies to discord_dm) are forwarded to the owners' DMs, untouched by any model.

- A message in one of `channel_ids` starts a Discord thread and a pmjc thread. Replies in that Discord thread
  continue the conversation; the last `history_turns` exchanges go back to the model. A message in any thread under
  a door channel (also one made in the Discord UI) starts its own pmjc thread the same way.
- Slash commands (/tool, /pmjc; see discord_commands) and an optional per-thread tool trace.
- A DM to the bot works the same way, one conversation per DM until you send `new`.
- Each message runs through ask() (connectors, watches, jobs) with the configured scopes. Act tools still need
  approval: the door posts Approve/Deny buttons in the conversation, and only an owner's click counts.
- With the door running, Discord notifications for other approvals use the same buttons instead of links.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import random
import time

from . import agent, config, creds, db, defang, executor, grants, memory, registry, router, routing, settings_edit
from .auth import Caller

log = logging.getLogger("pmjc.discord_door")

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)  # guilds (threads), guild messages, direct messages, message content
FATAL_CLOSE = {4004: "invalid bot token", 4010: "invalid shard", 4011: "sharding required", 4012: "bad API version",
               4013: "invalid intents", 4014: "Message Content intent not enabled in the Developer Portal"}
NEW_WORDS = {"new", "/new", "!new", "reset"}
MAX_MSG = 2000
SUPPRESS_EMBEDS = 4  # message flag: Discord doesn't fetch or unfurl links in the message
DDL = "CREATE TABLE IF NOT EXISTS discord_door (channel_id TEXT PRIMARY KEY, thread_id TEXT, created REAL)"
PREFS_DDL = "CREATE TABLE IF NOT EXISTS discord_door_prefs (channel_id TEXT PRIMARY KEY, prefs TEXT)"
BUTTON_PREFIX = "pmjc:"
FRIENDS_NOTE = ("You are talking to your owner through Discord. To message someone else you can use discord_dm (sent "
                "AS THE BOT, only to people who share a server with it; pass contact=<the name the owner used>, it "
                "finds the person in the bot's servers itself, so don't ask for user ids) or pmjc_handoff (a one-tap link so the owner "
                "sends it as themselves). Never claim to have sent something as the owner. Both are act tools, so the "
                "owner approves each one.")

_door: Door | None = None

# ---- per-run log capture (posted to discord_door.log_channel_id) -----------------------------------------
_RUN_LOG: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("pmjc_door_run_log", default=None)


class _RunLogHandler(logging.Handler):
    """Collects pmjc log lines emitted while a door run is active (tasks it starts inherit the context)."""

    def emit(self, record: logging.LogRecord) -> None:
        buf = _RUN_LOG.get()
        if buf is not None and len(buf) < 400:
            with contextlib.suppress(Exception):
                buf.append(creds.Redactor.scrub(self.format(record)))


_handler = _RunLogHandler(logging.INFO)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))


def _install_handler() -> None:
    lg = logging.getLogger("pmjc")  # pmjc's own loggers only: httpx lines would carry interaction tokens in URLs
    if _handler not in lg.handlers:
        lg.addHandler(_handler)


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


def approval_buttons(aid: str, reviewed: bool = False) -> list[dict]:
    """Approve/Deny. A settings change in the routing or cost class gets a two-step confirm: the first button is
    Review (it edits the message and then offers Approve)."""
    two_step = False
    if not reviewed:
        rows = db.q("SELECT tool, args, reason FROM approvals WHERE id=?", aid)
        two_step = bool(rows and settings_edit.needs_review(rows[0]))
    first = ({"type": 2, "style": 1, "label": "Review", "custom_id": f"{BUTTON_PREFIX}r:{aid}"} if two_step else
             {"type": 2, "style": 3, "label": "Approve", "custom_id": f"{BUTTON_PREFIX}a:{aid}"})
    return [{"type": 1, "components": [first,
        {"type": 2, "style": 4, "label": "Deny", "custom_id": f"{BUTTON_PREFIX}d:{aid}"}]}]


def route_buttons(choice: dict) -> list[dict]:
    """GPU busy: one button per offered route (approving it runs the request there); waiting needs no button."""
    btns = [{"type": 2, "style": 1 if o["id"] == "cloud" else 2, "custom_id": f"{BUTTON_PREFIX}a:{o['approval_id']}",
             "label": "Use a cloud model" if o["id"] == "cloud" else "Use local anyway"}
            for o in choice.get("options", []) if o.get("approval_id")]
    return [{"type": 1, "components": btns}] if btns else []


def approval_text(a: dict, reviewed: bool = False) -> str:
    if a.get("tool") in settings_edit.TOOLS and settings_edit.view(a):  # an escaped old -> new diff, not raw JSON
        return settings_edit.discord_text(a, reviewed)
    if grants.view(a):  # a run grant: pmjc's summary of the rules, then the model's quoted intent
        return grants.discord_text(a)
    args = a.get("args") or "{}"
    limit = 1700 if a.get("tool") == "pmjc_shell_run" else 900  # a shell command is always shown whole
    if len(args) > limit:
        args = args[:limit] + "..."
    tainted = bool(a.get("tainted"))  # proposed by a run that read untrusted content: its links are defanged
    args = defang.text(args, tainted, wrap=False).replace("```", "'" * 3)
    reason = f"\n{defang.text(a['reason'], tainted)}" if a.get("reason") else ""
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
        self._deciding: set[str] = set()  # approvals whose button click is being handled (double-click guard)
        _install_handler()
        self.app_id = ""
        self.parents: dict[str, str] = {}  # thread id -> parent channel id
        self.guilds: dict[str, str] = {}  # channel id -> guild id
        db.x(DDL)
        db.x(PREFS_DDL)
        from .discord_commands import Commands
        self.commands = Commands(self)

    # ---- conversation map
    def thread_for(self, channel_id: str) -> str | None:
        r = db.q("SELECT thread_id FROM discord_door WHERE channel_id=?", channel_id)
        return r[0]["thread_id"] if r else None

    def bind(self, channel_id: str, thread_id: str) -> None:
        db.x("INSERT OR REPLACE INTO discord_door(channel_id, thread_id, created) VALUES(?,?,?)",
             channel_id, thread_id, time.time())

    def unbind(self, channel_id: str) -> None:
        db.x("DELETE FROM discord_door WHERE channel_id=?", channel_id)

    def prefs(self, channel_id: str) -> dict:
        r = db.q("SELECT prefs FROM discord_door_prefs WHERE channel_id=?", channel_id)
        return json.loads(r[0]["prefs"]) if r else {}

    def set_pref(self, channel_id: str, key: str, value) -> None:
        p = self.prefs(channel_id)
        p[key] = value
        db.x("INSERT OR REPLACE INTO discord_door_prefs(channel_id, prefs) VALUES(?,?)", channel_id, json.dumps(p))

    async def channel_info(self, channel_id: str) -> dict:
        try:
            info = await self.api("GET", f"/channels/{channel_id}", None) or {}
        except Exception:
            log.warning("could not look up channel %s", channel_id)
            return {}
        if info.get("parent_id"):
            self.parents[channel_id] = info["parent_id"]
        if info.get("guild_id"):
            self.guilds[channel_id] = info["guild_id"]
        return info

    async def parent_of(self, channel_id: str) -> str | None:
        if channel_id not in self.parents:
            await self.channel_info(channel_id)
        return self.parents.get(channel_id)

    async def guild_of(self, channel_id: str) -> str | None:
        if channel_id not in self.guilds:
            await self.channel_info(channel_id)
        return self.guilds.get(channel_id)

    async def in_door_thread(self, channel_id: str) -> bool:
        """A Discord thread under a door channel (or one already bound to a pmjc thread)."""
        return self.thread_for(channel_id) is not None or await self.parent_of(channel_id) in self.cfg.channel_ids

    async def close_thread(self, channel_id: str) -> None:
        tid = self.thread_for(channel_id)
        if tid:
            with contextlib.suppress(ValueError):
                memory.thread_set_status(tid, "closed")
        self.unbind(channel_id)

    def is_owner(self, user_id: str | None) -> bool:
        return bool(user_id) and str(user_id) in {str(u) for u in self.cfg.owner_user_ids}

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ---- dispatch events
    async def on_message(self, m: dict) -> None:
        author = m.get("author") or {}
        if author.get("bot"):
            return
        if not self.is_owner(author.get("id")):
            if not m.get("guild_id") and self.cfg.forward_dms:  # someone DMed the bot (e.g. a reply to discord_dm): pass it on, never act on it
                self.spawn(self.forward_dm(m, author))
            return  # never act on anyone else's messages

        if m.get("type", 0) not in (0, 19):
            return  # system messages (e.g. "started a thread") are not requests
        text = (m.get("content") or "").strip()
        attachments = [a.get("url") for a in m.get("attachments", []) if a.get("url")]
        if attachments:
            text += "\n(attachments: " + ", ".join(attachments) + ")"
        if not text:
            return
        ch = m["channel_id"]
        is_dm = not m.get("guild_id")
        if self.thread_for(ch) is None and not is_dm and ch not in self.cfg.channel_ids \
                and await self.parent_of(ch) not in self.cfg.channel_ids:
            return  # a channel or thread the door doesn't own
        lock = self._locks.setdefault(ch, asyncio.Lock())
        async with lock:  # one run at a time per conversation, in order
            await self.logged(f"message in <#{ch}>: {text[:200]}", self._handle(m, ch, is_dm, text, author["id"]))

    async def _handle(self, m: dict, ch: str, is_dm: bool, text: str, user_id: str) -> None:
        tid = self.thread_for(ch)
        reply_ch = ch
        new_thread = False
        if is_dm and text.lower() in NEW_WORDS:
            self.unbind(ch)
            await self.send(ch, "Started a new conversation.")
            return
        if tid is None and not is_dm and ch in self.cfg.channel_ids:  # new message in a door channel: its own thread
            name = " ".join(text.split())[:90] or "pmjc"
            t = await self.api("POST", f"/channels/{ch}/messages/{m['id']}/threads",
                               {"name": name, "auto_archive_duration": 1440})
            reply_ch = t["id"]
            new_thread = True
        caller = Caller(f"discord:{user_id}", "remote", set(self.cfg.scopes), surface="discord")
        prefs = self.prefs(reply_ch)
        trace = bool(prefs.get("trace", self.cfg.trace))
        caller.on_tool = lambda name, args, status: self._on_tool(reply_ch, trace, name, args, status)
        started = time.time()
        typing = asyncio.create_task(self._typing(reply_ch))
        try:
            from . import ask as ask_mod
            res = await ask_mod.ask(caller, text, private=bool(prefs.get("private", self.cfg.private)), thread_id=tid,
                                    history_turns=self.cfg.history_turns, extra_system=FRIENDS_NOTE,
                                    provider_order=[prefs["provider"]] if prefs.get("provider") else None)
        except agent.NoProvider as e:
            log.warning("no provider: %s", e)
            await self.send(reply_ch, f"No model is available right now: {e}")
            return
        except Exception as e:
            log.exception("discord door run failed")
            await self.send(reply_ch, f"That failed: {type(e).__name__}: {str(e)[:300]}")
            return
        finally:
            typing.cancel()
        if res.get("route_choice"):  # GPU busy: nothing ran yet, the owner picks where
            if tid is None:
                self.bind(reply_ch, res["thread_id"])
            await self.send(reply_ch, res["answer"], components=route_buttons(res["route_choice"]), suppress_embeds=True)
            return
        if tid is None:
            self.bind(reply_ch, res["thread_id"])
            if new_thread and self.cfg.auto_title:
                self.spawn(self._retitle(reply_ch, res["thread_id"], text, res["answer"], res["provider"]))
        log.info("answered in <#%s> by %s (pmjc thread %s, %.1fs, $%.4f)%s", reply_ch, res["provider"], res["thread_id"],
                 time.time() - started, res.get("cost_usd") or 0,
                 "".join(f"; fell back from {f}" for f in res.get("fell_back") or []))
        footer = f"\n-# {res['provider']}"
        if res.get("watches"):
            footer += f" · watching ({', '.join(res['watches'])})"
        if res.get("cost_usd"):
            footer += f" · ${res['cost_usd']:.4f}"
        if routing.footer_enabled("discord"):  # the routing footer replaces the plain provider line
            footer = "\n-# " + routing.footer_line(res["provider"], res.get("routing"), res.get("cost_usd") or 0)
            if res.get("watches"):
                footer += f" · watching ({', '.join(res['watches'])})"
        for f in res.get("fell_back") or []:
            footer += f"\n-# fell back from {f[:150]}"
        quiet = bool(res.get("untrusted"))  # tainted answer: no link previews (flag SUPPRESS_EMBEDS)
        for part in chunks(res["answer"] + footer):
            await self.send(reply_ch, part, suppress_embeds=quiet)
        for a in db.q("SELECT id, tool, args, reason, tainted FROM approvals WHERE thread_id=? AND status='pending' AND created>=?"
                      " AND tool!='pmjc_route_override'", res["thread_id"], started - 1):
            await self.send(reply_ch, approval_text(a), components=approval_buttons(a["id"]), suppress_embeds=True)

    async def _retitle(self, channel_id: str, thread_id: str, request: str, answer: str, provider: str) -> None:
        """Rename a thread the door started (named after the raw message) to a short topic, like chat apps do.
        Uses the provider that just answered, without tools; failures keep the old name."""
        prompt = ("Write a title of 2 to 6 words for this conversation. Reply with the title only: no quotes, "
                  "no trailing punctuation.\n\nUser: " + request[:1500] + "\n\nAssistant: " + answer[:1500])
        try:
            res = await agent.run(prompt, Caller("discord:title", "remote", set()), [],
                                  router.Need(order=[provider], capabilities=set()), system="You name conversations.",
                                  max_turns=1)
            title = " ".join(res.text.strip().strip('"\'`*#').split())[:90]
            if not title or "\n" in res.text.strip():
                return
            await self.api("PATCH", f"/channels/{channel_id}", {"name": title})
            memory.thread_set_title(thread_id, title)
            log.info("thread <#%s> renamed to %r", channel_id, title)
        except Exception as e:
            log.info("thread rename skipped: %s", type(e).__name__)

    async def _on_tool(self, channel_id: str, trace: bool, name: str, args: dict, status: str) -> None:
        from .discord_commands import clip
        log.info("tool %s(%s) -> %s", name, clip(json.dumps(args, default=str), 300) if args else "", status)
        if trace:
            await self._trace(channel_id, name, args, status)

    async def logged(self, title: str, coro):
        """Run `coro` and post everything pmjc logged during it to the log channel (if one is set)."""
        buf: list[str] = []
        token = _RUN_LOG.set(buf)
        t0, error = time.time(), None
        try:
            return await coro
        except Exception as e:
            error = e
            raise
        finally:
            _RUN_LOG.reset(token)
            if self.cfg.log_channel_id:
                self.spawn(self._post_log(title, buf, time.time() - t0, error))

    async def _post_log(self, title: str, lines: list[str], secs: float, error: Exception | None) -> None:
        head = f"**{creds.Redactor.scrub(title)[:300]}** · {secs:.1f}s" + (f" · **{type(error).__name__}**" if error else "")
        body = "\n".join(lines) or "(nothing logged)"
        parts = chunks(body, MAX_MSG - 400)
        with contextlib.suppress(Exception):
            for n, part in enumerate(parts):
                await self.send(self.cfg.log_channel_id, (head + "\n" if n == 0 else "") + "```\n" + part.replace("```", "'''") + "\n```")

    async def _trace(self, channel_id: str, name: str, args: dict, status: str) -> None:
        from .discord_commands import clip
        shown = clip(json.dumps(args, default=str), 80) if args else ""
        await self.send(channel_id, f"-# tool {name}({shown}) -> {status}")

    async def on_interaction(self, i: dict) -> None:
        if i.get("type") == 4:  # autocomplete: too chatty to log
            await self.commands.handle(i)
            return
        if i.get("type") in (2, 3):
            data = i.get("data") or {}
            await self.logged(f"{'/' + data.get('name', '') if i['type'] == 2 else 'button ' + data.get('custom_id', '')}"
                              f" in <#{i.get('channel_id', '')}>", self._interaction(i))
            return

    async def _interaction(self, i: dict) -> None:
        if i.get("type") == 2:  # slash command
            log.info("slash command %s", json.dumps((i.get("data") or {}).get("options") or [], default=str)[:500])
            await self.commands.handle(i)
            return
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
        if kind in ("del", "keep"):
            await self._delete_choice(i, cb, kind == "del", aid)
            return
        if kind == "r":
            await self._review_button(i, cb, aid)
            return
        await self._decide_button(i, cb, kind == "a", aid, user)

    async def _review_button(self, i: dict, cb: str, aid: str) -> None:
        """Step one of a two-step settings approval: show the diff again as reviewed and offer Approve."""
        row = db.q("SELECT tool, args, reason, status FROM approvals WHERE id=?", aid)
        if not row or row[0]["status"] != "pending":
            await self._ephemeral(cb, f"Already {row[0]['status'] if row else 'gone'}.")
            return
        await self.api("POST", cb, {"type": 7, "data": {"content": approval_text(row[0], reviewed=True)[:MAX_MSG],
                                                          "components": approval_buttons(aid, reviewed=True),
                                                          "allowed_mentions": {"parse": []}}})

    def _ephemeral(self, cb: str, text: str):
        return self.api("POST", cb, {"type": 4, "data": {"content": text, "flags": 64}})

    async def _decide_button(self, i: dict, cb: str, approve: bool, aid: str, user: dict) -> None:
        """Approve/Deny: answer the click at once (type 7 update: the buttons go away and the message says what is
        happening), then decide in the background and edit the message with the outcome. A second click on an
        approval that is already being decided or decided gets a private note and never runs anything again.
        The check and the claim happen before the first await, so two quick clicks can't both pass."""
        if aid in self._deciding:
            await self._ephemeral(cb, "Already approved, running..." if approve else "Already being decided...")
            return
        row = db.q("SELECT tool, args, reason, status FROM approvals WHERE id=?", aid)
        if row and row[0]["status"] != "pending":
            await self._ephemeral(cb, f"Already {row[0]['status']}.")
            return
        if (approve and row and settings_edit.needs_review(row[0])
                and settings_edit.REVIEWED_MARK not in ((i.get("message") or {}).get("content") or "")):
            await self._ephemeral(cb, "Press Review first, then Approve.")
            return
        self._deciding.add(aid)
        tool_name = row[0]["tool"] if row else "the action"
        body = ((i.get("message") or {}).get("content") or "")[:1200]
        who = f"<@{user['id']}>"
        working = body + f"\n{'✅ Approved' if approve else '🚫 Denied'} by {who}" + (" — running…" if approve else "")
        try:
            await self.api("POST", cb, {"type": 7, "data": {"content": working[:MAX_MSG], "components": [],
                                                              "allowed_mentions": {"parse": []}}})
        except Exception:
            self._deciding.discard(aid)
            raise
        self.spawn(self._finish_decision(i, aid, approve, user, tool_name, body))

    async def _finish_decision(self, i: dict, aid: str, approve: bool, user: dict, tool_name: str, body: str) -> None:
        try:
            await self.logged(f"approval {'approve' if approve else 'deny'} of {tool_name}",
                              self._decide_and_edit(i, aid, approve, user, tool_name, body))
        finally:
            self._deciding.discard(aid)

    async def _decide_and_edit(self, i: dict, aid: str, approve: bool, user: dict, tool_name: str, body: str) -> None:
        try:
            res = await executor.decide(aid, approve, f"discord:{user['id']}")
        except Exception as e:  # e.g. Denied: tampered payload
            res = {"status": "failed", "error": f"{type(e).__name__}: {str(e)[:300]}"}
        status = res.get("status", "?")
        detail = ""
        if "result" in res and res["result"] is not None:
            detail = json.dumps(res["result"], default=str) if not isinstance(res["result"], str) else res["result"]
        elif res.get("error"):
            detail = res["error"]
        text = body + f"\n**{status}** by <@{user['id']}>" + (f"\n```\n{detail[:600]}\n```" if detail else "")
        edit = {"content": text[:MAX_MSG], "components": [], "allowed_mentions": {"parse": []}}
        app_id = i.get("application_id") or self.bot_id
        try:
            await self.api("PATCH", f"/webhooks/{app_id}/{i['token']}/messages/@original", edit)
        except Exception:  # the interaction token lasts 15 minutes; fall back to editing the message as the bot
            mid, ch0 = (i.get("message") or {}).get("id"), i.get("channel_id")
            if mid and ch0:
                with contextlib.suppress(Exception):
                    await self.api("PATCH", f"/channels/{ch0}/messages/{mid}", edit)
        ch = i.get("channel_id")
        if ch and tool_name == "pmjc_route_override":  # a routing choice: run the original request again, there
            if status == "done" and self.thread_for(ch) is not None:
                req = json.loads(db.q("SELECT args FROM approvals WHERE id=?", aid)[0]["args"]).get("request", "")
                if req:
                    self.spawn(self._continue(ch, not i.get("guild_id"), req, user["id"]))
            return
        if ch and status in ("done", "denied", "failed") and self.thread_for(ch) is not None:
            from . import ask as ask_mod
            text = ask_mod.outcome_text(tool_name, status, detail)
            self.spawn(self._continue(ch, not i.get("guild_id"), text, user["id"]))

    async def drain(self) -> None:
        """Wait for background work (approval decisions, follow-up turns). For tests and shutdown."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _continue(self, ch: str, is_dm: bool, text: str, user_id: str) -> None:
        """Feed an approval outcome back into the conversation in channel ch, as the next turn."""
        lock = self._locks.setdefault(ch, asyncio.Lock())
        async with lock:
            await self.logged(f"approval result in <#{ch}>", self._handle({}, ch, is_dm, text, user_id))

    async def _delete_choice(self, i: dict, cb: str, confirm: bool, channel_id: str) -> None:
        if not confirm:
            await self.api("POST", cb, {"type": 7, "data": {"content": "Kept.", "components": []}})
            return
        await self.api("POST", cb, {"type": 7, "data": {"content": "Deleting...", "components": []}})
        await self.close_thread(channel_id)
        await self.api("DELETE", f"/channels/{channel_id}", None)

    # ---- REST helpers
    async def forward_dm(self, m: dict, author: dict) -> None:
        """Copy a non-owner's DM to each owner's DM, titled with who sent it. If a pmjc thread messaged this person
        (discord_dm), the reply is also added to that thread as untrusted content, so its next run sees it. Nothing
        is run in response: a model only reads it when the owner continues that thread."""
        name = author.get("global_name") or author.get("username") or "someone"
        uid = str(author.get("id", "?"))
        title = f"**DM from {name}** (@{author.get('username', '?')}, id {uid})"
        body = (m.get("content") or "").strip()
        urls = [a.get("url") for a in m.get("attachments", []) if a.get("url")]
        if urls:
            body += ("\n" if body else "") + "attachments: " + " ".join(urls)
        tid = memory.reply_route_get(f"discord:{uid}")
        if tid:
            with contextlib.suppress(Exception):
                memory.thread_add(tid, "reply", f"Discord DM reply from {name} (@{author.get('username', '?')}): {body}",
                                  author=f"discord:{uid}", trust="untrusted", status="updated", unread=True)
                r = db.q("SELECT title FROM threads WHERE id=?", tid)
                title += f" re: {r[0]['title'] if r and r[0]['title'] else tid}"
        text = title + "\n" + ("\n".join("> " + ln for ln in body.splitlines()) or "> (empty message)")
        for owner in self.cfg.owner_user_ids:
            try:
                dm = await self.api("POST", "/users/@me/channels", {"recipient_id": str(owner)})
                await self.send(dm["id"], text)
            except Exception:
                log.warning("could not forward a DM from %s to owner %s", author.get("id"), owner)

    async def send(self, channel_id: str, content: str, components: list | None = None,
                   suppress_embeds: bool = False) -> dict:
        body = {"content": content[:MAX_MSG], "allowed_mentions": {"parse": []}}
        if suppress_embeds:
            body["flags"] = SUPPRESS_EMBEDS
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
            self.door.app_id = (d.get("application") or {}).get("id") or self.door.bot_id
            self.door.connected = True
            log.info("discord door connected as %s", d["user"].get("username"))
            self.door.spawn(self._guard(self.door.commands.register()))
        elif t == "THREAD_CREATE" and d.get("parent_id"):
            self.door.parents[d["id"]] = d["parent_id"]
        elif t == "THREAD_DELETE":
            self.door.spawn(self._guard(self.door.close_thread(d["id"])))
            self.door.parents.pop(d["id"], None)
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
