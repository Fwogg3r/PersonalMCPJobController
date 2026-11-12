"""Chat platforms: Discord (bot), Telegram (bot), Slack (bot). Bots only see places they've been added to."""
from __future__ import annotations

import httpx

from .. import config, memory
from ..connector import Connector, Field, Health, tool


MEMBERS_INTENT = ("the bot can't list server members: turn on 'Server Members Intent' for the bot in the Discord "
                  "Developer Portal (Bot tab)")


class Discord(Connector):
    id = "discord"
    group = "social"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['discord', 'dm', 'direct message']  # request words that select this connector (tool_mode: select)
    name = "Discord (bot)"
    description = ("Read and post in servers where your bot is a member (enable the Message Content intent). "
                   "Bots can't read your personal DMs; use Windows notifications for those.")
    limitations = ("Acts as the bot, never as the user. Sees only servers/channels the bot was added to; DMs only people "
                   "who share a server with it. Can't see anyone's friends list. Channel ids come from discord_channels. "
                   "Attachment URLs expire in ~1 day. Finding people by name needs the Server Members intent.")
    native_alternatives = "a messaging connector or browser control signed in as the user, to talk as the user."
    docs_url = "https://discord.com/developers/applications"
    credentials = [Field("bot_token", "Bot token"),
                   Field("notify_channel_id", "Channel for pmjc notifications", secret=False, required=False)]
    API = "https://discord.com/api/v10"

    async def _api(self, method, path, json=None, params=None):
        r = await self.http.request(method, self.API + path, json=json, params=params,
                                    headers={"Authorization": f"Bot {self.secret('bot_token')}"})
        r.raise_for_status()
        return r.json() if r.content else {}

    @tool(scope="read")
    async def discord_channels(self, guild_id: str | None = None) -> list[dict]:
        """Servers the bot is in (no argument), or the text channels of one server (guild_id from the first call). Channel ids for discord_read_channel and discord_send come from here."""
        if not guild_id:
            return [{"id": g["id"], "name": g["name"]} for g in await self._api("GET", "/users/@me/guilds")]
        return [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in await self._api("GET", f"/guilds/{guild_id}/channels")
                if c["type"] in (0, 5, 15)]

    @tool(scope="read", untrusted_output=True)
    async def discord_read_channel(self, channel_id: str, limit: int = 30) -> list[dict]:
        """Recent messages in a channel or thread, oldest first, with attachment URLs (they expire after ~24h). channel_id comes from discord_channels."""
        msgs = await self._api("GET", f"/channels/{channel_id}/messages", params={"limit": min(limit, 100)})
        return [{"id": m["id"], "author": m["author"].get("global_name") or m["author"]["username"],
                 "content": m["content"], "ts": m["timestamp"],
                 "attachments": [{"url": a["url"], "type": a.get("content_type"), "name": a["filename"]} for a in m.get("attachments", [])]}
                for m in reversed(msgs)]

    @tool(scope="act", level="always_ask")
    async def discord_send(self, channel_id: str, content: str) -> dict:
        """Post a message in a channel AS THE BOT, not as the user. channel_id comes from discord_channels."""
        m = await self._api("POST", f"/channels/{channel_id}/messages",
                            json={"content": content[:2000], "allowed_mentions": {"parse": []}})
        return {"id": m["id"]}

    async def _guild_members(self, guild_id: str, query: str | None) -> list[dict]:
        """Members of one server: the full list (needs the Server Members intent), else Discord's name search."""
        try:
            return await self._api("GET", f"/guilds/{guild_id}/members", params={"limit": 1000})
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 403 or not query:
                if e.response.status_code == 403:
                    raise ValueError(MEMBERS_INTENT) from e
                raise
        try:
            return await self._api("GET", f"/guilds/{guild_id}/members/search", params={"query": query[:100], "limit": 100})
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                raise ValueError(MEMBERS_INTENT) from e
            raise

    async def _members(self, query: str | None = None, guild_id: str | None = None) -> list[dict]:
        """People (no bots) in the bot's servers, one row per user; with a query, only the best name matches."""
        guilds = [{"id": guild_id, "name": ""}] if guild_id else await self._api("GET", "/users/@me/guilds")
        people: dict[str, dict] = {}
        for g in guilds:
            for m in await self._guild_members(g["id"], query):
                u = m.get("user") or {}
                if not u.get("id") or u.get("bot"):
                    continue
                p = people.setdefault(u["id"], {"user_id": u["id"], "username": u.get("username", ""),
                                                "display": u.get("global_name") or "", "nicks": [], "servers": []})
                if m.get("nick") and m["nick"] not in p["nicks"]:
                    p["nicks"].append(m["nick"])
                if g.get("name") and g["name"] not in p["servers"]:
                    p["servers"].append(g["name"])
        rows = list(people.values())
        if not query:
            return rows
        q = query.casefold().strip().lstrip("@")

        def score(p: dict) -> int:  # 3 exact name, 2 a name starts with it, 1 a name contains it
            names = [n.casefold() for n in (p["username"], p["display"], *p["nicks"]) if n]
            return 3 if q in names else 2 if any(n.startswith(q) for n in names) else 1 if any(q in n for n in names) else 0
        scored = [(score(p), p) for p in rows]
        best = max((s for s, _ in scored), default=0)
        return [p for s, p in scored if s and s == best]

    async def _find_person(self, names: list[str]) -> dict:
        """The one person in the bot's servers matching one of `names` (tried in order); refuses to guess."""
        names = [n for n in dict.fromkeys(names) if n]
        for n in names:
            found = await self._members(n)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ValueError(f"several people match {n!r}: " + "; ".join(
                    f"{p['display'] or p['username']} (@{p['username']}, user_id {p['user_id']})" for p in found[:10])
                    + ". Ask the user which one and pass user_id")
        raise ValueError(f"nobody in the bot's servers matches {' / '.join(names)!r}; the bot can only message people "
                         "who share a server with it")

    async def _dm_channel(self, user_id: str | None, contact: str | None) -> tuple[str, str, dict | None]:
        """Resolve the person and open the bot's DM with them. Order: user_id; a saved contact with a discord_user_id;
        else the one server member matching the contact's Discord names, name and aliases (or the name given).
        Returns (channel, user_id, found): `found` is the member row when the person was looked up in the servers."""
        found = None
        if not user_id:
            if not contact:
                raise ValueError("give user_id or contact (a contact name, an alias or the person's Discord name)")
            c = memory.contact_resolve(contact) or {}
            h = c.get("handles") or {}
            user_id = h.get("discord_user_id")
            if not user_id:
                found = await self._find_person([h.get("discord_username"), h.get("discord_display"), c.get("name"),
                                                 *(c.get("aliases") or []), contact])
                user_id = found["user_id"]
        if not str(user_id).isdigit():
            raise ValueError("user_id must be a numeric Discord id")
        ch = await self._api("POST", "/users/@me/channels", json={"recipient_id": str(user_id)})
        return ch["id"], str(user_id), found

    def _save_contact(self, contact: str, found: dict, caller) -> str:
        """After a DM to someone found by name, remember them (connectors.discord.save_contacts, default on). Runs as
        the owner when the DM was approved (the approval names the person), else as the calling run, so the save
        follows contacts_from_chat; a run that read outside content can't write contacts."""
        if not (config.load().connectors.get("discord") or {}).get("save_contacts", True):
            return "off"
        c = memory.contact_resolve(contact)
        handles = {"discord_user_id": found["user_id"], "discord_username": found["username"]}
        if found["display"]:
            handles["discord_display"] = found["display"]
        try:
            return memory.contact_set(c["name"] if c else contact.strip()[:100], None, handles, caller=caller).get("status", "saved")
        except Exception as e:  # the DM already went out; only the contact wasn't stored
            return f"not saved: {e}"[:200]

    @tool(scope="read", untrusted_output=True)
    async def discord_members(self, query: str | None = None, guild_id: str | None = None) -> list[dict]:
        """Find people in the servers the bot is in, by Discord username, display name or server nickname (best
        matches only; no bots). Without a query, lists everyone. Rows: user_id, username, display, nicks, servers.
        discord_dm(contact=<name>) already looks people up by itself. Bots can't see anyone's friends list."""
        return (await self._members(query, guild_id))[:50]

    @tool(scope="act", level="always_ask")
    async def discord_dm(self, content: str, user_id: str | None = None, contact: str | None = None, _caller=None) -> dict:
        """Send a direct message AS THE BOT (not as the user) to someone who shares a server with the bot. Pass
        contact=<the name the user said> ("Emme", or an alias such as "Anti"): pmjc checks saved contacts, else finds
        the one matching member of the bot's servers and saves them as a contact. Or give a numeric user_id. If several
        people match, the error lists them: ask the user which. To send as the user themselves, use pmjc_handoff."""
        channel, uid, found = await self._dm_channel(user_id, contact)
        m = await self._api("POST", f"/channels/{channel}/messages",
                            json={"content": content[:2000], "allowed_mentions": {"parse": []}})
        out = {"id": m["id"], "dm_channel_id": channel, "user_id": uid, "reply_route": f"discord:{uid}"}
        if found:
            out["to"] = f"{found['display'] or found['username']} (@{found['username']})"
            out["contact"] = self._save_contact(contact or "", found, _caller)
        return out

    @tool(scope="read", untrusted_output=True)
    async def discord_dm_read(self, user_id: str | None = None, contact: str | None = None, limit: int = 30) -> list[dict]:
        """Recent messages in the bot's DM with a person (the bot's own conversation, not the user's personal DMs),
        oldest first. Give user_id or a contact name/alias with a discord_user_id handle."""
        channel, _, _ = await self._dm_channel(user_id, contact)
        return await self.discord_read_channel(channel, limit)

    async def notify(self, title, message, actions=None):
        ch = self.secret("notify_channel_id")
        from .. import discord_door
        aid = next((a["path"].split("/approve/")[1].split("?")[0] for a in (actions or [])
                    if "/approve/" in a.get("path", "")), None)
        if aid and discord_door.running():  # the door handles button clicks; no public URL needed
            await self._api("POST", f"/channels/{ch}/messages",
                            json={"content": f"**{title}**\n{message}"[:2000], "allowed_mentions": {"parse": []},
                                  "flags": 4, "components": discord_door.approval_buttons(aid)})
            return
        public = config.load().public_url.rstrip("/")
        links = "".join(f"\n{a['title']}: <{public}{a['path']}>" for a in (actions or []) if public)
        # notifications carry job, watch and approval text that may come from a tainted run: never unfurl links (flags 4)
        await self._api("POST", f"/channels/{ch}/messages", json={
            "content": f"**{title}**\n{message}{links}"[:2000], "allowed_mentions": {"parse": []}, "flags": 4})


class Telegram(Connector):
    id = "telegram"
    group = "social"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['telegram']  # request words that select this connector (tool_mode: select)
    limitations = 'Acts as the bot, never as the user; reads only messages sent to the bot.'
    native_alternatives = 'a Telegram connector signed in as the user.'
    name = "Telegram (bot)"
    description = "Send yourself notifications and read messages sent to your bot. Create a bot with @BotFather."
    docs_url = "https://core.telegram.org/bots/api"
    credentials = [Field("bot_token", "Bot token"),
                   Field("chat_id", "Your chat id", secret=False, help="Message the bot, then run: pmjc telegram chat-id")]

    async def _api(self, method: str, **body):
        r = await self.http.post(f"https://api.telegram.org/bot{self.secret('bot_token')}/{method}", json=body)
        r.raise_for_status()
        return r.json()["result"]

    @tool(scope="read", untrusted_output=True)
    async def telegram_updates(self, limit: int = 20) -> list[dict]:
        """Recent messages sent to the bot."""
        ups = await self._api("getUpdates", limit=min(limit, 100), timeout=0)
        return [{"chat_id": u["message"]["chat"]["id"], "from": u["message"].get("from", {}).get("username"),
                 "text": u["message"].get("text", ""), "ts": u["message"]["date"]} for u in ups if "message" in u]

    @tool(scope="act", level="always_ask")
    async def telegram_send(self, text: str, chat_id: str | None = None) -> dict:
        """Send a Telegram message as the bot (to the user's own chat by default; chat_id otherwise)."""
        m = await self._api("sendMessage", chat_id=chat_id or self.secret("chat_id"), text=text[:4000],
                            link_preview_options={"is_disabled": True})
        return {"message_id": m["message_id"]}

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        body = {"chat_id": self.secret("chat_id"), "text": f"{title}\n{message}"[:4000],
                "link_preview_options": {"is_disabled": True}}
        if actions and public:
            body["reply_markup"] = {"inline_keyboard": [[{"text": a["title"], "url": public + a["path"]} for a in actions]]}
        await self._api("sendMessage", **body)


class Slack(Connector):
    id = "slack"
    group = "social"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['slack']  # request words that select this connector (tool_mode: select)
    limitations = 'Acts as the app, never as the user; sees only channels it was invited to.'
    native_alternatives = 'a Slack connector signed in as the user.'
    name = "Slack (bot)"
    description = "Read and post in channels your Slack app has joined."
    docs_url = "https://api.slack.com/apps"
    credentials = [Field("bot_token", "Bot token (xoxb-...)"),
                   Field("notify_channel", "Channel id for notifications", secret=False, required=False)]

    async def _api(self, method: str, **params):
        r = await self.http.post(f"https://slack.com/api/{method}", json=params,
                                 headers={"Authorization": f"Bearer {self.secret('bot_token')}"})
        r.raise_for_status()
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError(f"slack {method}: {d.get('error')}")
        return d

    @tool(scope="read")
    async def slack_channels(self) -> list[dict]:
        """Channels the app can see."""
        d = await self._api("conversations.list", limit=200, exclude_archived=True)
        return [{"id": c["id"], "name": c["name"], "member": c.get("is_member")} for c in d["channels"]]

    @tool(scope="read", untrusted_output=True)
    async def slack_read_channel(self, channel_id: str, limit: int = 30) -> list[dict]:
        """Recent messages in a channel, oldest first. channel_id comes from slack_channels."""
        d = await self._api("conversations.history", channel=channel_id, limit=min(limit, 200))
        return [{"user": m.get("user"), "text": m.get("text", ""), "ts": m["ts"]} for m in reversed(d["messages"])]

    @tool(scope="act", level="always_ask")
    async def slack_send(self, channel_id: str, text: str) -> dict:
        """Post a message in a channel as the app, not as the user. channel_id comes from slack_channels."""
        d = await self._api("chat.postMessage", channel=channel_id, text=text[:4000], unfurl_links=False, unfurl_media=False)
        return {"ts": d["ts"]}

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        links = "".join(f"\n<{public}{a['path']}|{a['title']}>" for a in (actions or []) if public)
        await self.slack_send(self.secret("notify_channel"), f"*{title}*\n{message}{links}")


class Ntfy(Connector):
    id = "ntfy"
    group = "notify"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['ntfy', 'push notification']  # request words that select this connector (tool_mode: select)
    limitations = 'Push notifications to the user only; one-way.'
    name = "ntfy (push notifications)"
    description = "Free push notifications to the ntfy phone app. Pick a long random topic name; it acts as a password."
    docs_url = "https://docs.ntfy.sh/"
    credentials = [Field("topic", "Topic"), Field("server", "Server", secret=False, required=False, default="https://ntfy.sh"),
                   Field("token", "Access token (optional)", required=False)]

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        headers = {"Title": title.encode("ascii", "ignore").decode()}
        tok = self.secret("token", required=False)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        if actions and public:
            headers["Actions"] = "; ".join(f"view, {a['title']}, {public}{a['path']}" for a in actions)
        r = await self.http.post(f"{self.secret('server').rstrip('/')}/{self.secret('topic')}",
                                 content=message.encode(), headers=headers)
        r.raise_for_status()


class Webhook(Connector):
    id = "webhook"
    group = "notify"
    effort_hint = "low"  # thinking effort for runs that use it (thinking.policy adaptive; see pmjc.effort)
    aliases = ['webhook']  # request words that select this connector (tool_mode: select)
    limitations = 'Posts to one configured URL; one-way.'
    name = "Outgoing webhook"
    description = "POSTs notifications as JSON to any URL (n8n, Zapier, Make, Home Assistant webhooks, IFTTT, ...)."
    credentials = [Field("url", "Webhook URL")]

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        r = await self.http.post(self.secret("url"), json={
            "title": title, "message": message,
            "actions": [{"title": a["title"], "url": public + a["path"] if public else a["path"]} for a in actions or []]})
        r.raise_for_status()
