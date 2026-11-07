"""Chat platforms: Discord (bot), Telegram (bot), Slack (bot). Bots only see places they've been added to."""
from __future__ import annotations

from .. import config
from ..connector import Connector, Field, Health, tool


class Discord(Connector):
    id = "discord"
    name = "Discord (bot)"
    description = ("Read and post in servers where your bot is a member (enable the Message Content intent). "
                   "Bots can't read your personal DMs; use Windows notifications for those.")
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
        """Servers the bot is in, or the text channels of one server."""
        if not guild_id:
            return [{"id": g["id"], "name": g["name"]} for g in await self._api("GET", "/users/@me/guilds")]
        return [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in await self._api("GET", f"/guilds/{guild_id}/channels")
                if c["type"] in (0, 5, 15)]

    @tool(scope="read", untrusted_output=True)
    async def discord_read_channel(self, channel_id: str, limit: int = 30) -> list[dict]:
        """Recent messages in a channel or thread, oldest first, with image/attachment URLs (URLs expire after ~24h)."""
        msgs = await self._api("GET", f"/channels/{channel_id}/messages", params={"limit": min(limit, 100)})
        return [{"id": m["id"], "author": m["author"].get("global_name") or m["author"]["username"],
                 "content": m["content"], "ts": m["timestamp"],
                 "attachments": [{"url": a["url"], "type": a.get("content_type"), "name": a["filename"]} for a in m.get("attachments", [])]}
                for m in reversed(msgs)]

    @tool(scope="act")
    async def discord_send(self, channel_id: str, content: str) -> dict:
        """Post a message in a channel."""
        m = await self._api("POST", f"/channels/{channel_id}/messages",
                            json={"content": content[:2000], "allowed_mentions": {"parse": []}})
        return {"id": m["id"]}

    async def notify(self, title, message, actions=None):
        ch = self.secret("notify_channel_id")
        public = config.load().public_url.rstrip("/")
        links = "".join(f"\n{a['title']}: <{public}{a['path']}>" for a in (actions or []) if public)
        await self.discord_send(ch, f"**{title}**\n{message}{links}")


class Telegram(Connector):
    id = "telegram"
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

    @tool(scope="act")
    async def telegram_send(self, text: str, chat_id: str | None = None) -> dict:
        """Send a Telegram message (to your own chat by default)."""
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
        """Recent messages in a channel, oldest first."""
        d = await self._api("conversations.history", channel=channel_id, limit=min(limit, 200))
        return [{"user": m.get("user"), "text": m.get("text", ""), "ts": m["ts"]} for m in reversed(d["messages"])]

    @tool(scope="act")
    async def slack_send(self, channel_id: str, text: str) -> dict:
        """Post a message in a channel."""
        d = await self._api("chat.postMessage", channel=channel_id, text=text[:4000], unfurl_links=False, unfurl_media=False)
        return {"ts": d["ts"]}

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        links = "".join(f"\n<{public}{a['path']}|{a['title']}>" for a in (actions or []) if public)
        await self.slack_send(self.secret("notify_channel"), f"*{title}*\n{message}{links}")


class Ntfy(Connector):
    id = "ntfy"
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
    name = "Outgoing webhook"
    description = "POSTs notifications as JSON to any URL (n8n, Zapier, Make, Home Assistant webhooks, IFTTT, ...)."
    credentials = [Field("url", "Webhook URL")]

    async def notify(self, title, message, actions=None):
        public = config.load().public_url.rstrip("/")
        r = await self.http.post(self.secret("url"), json={
            "title": title, "message": message,
            "actions": [{"title": a["title"], "url": public + a["path"] if public else a["path"]} for a in actions or []]})
        r.raise_for_status()
