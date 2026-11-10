"""Google: Gmail and Calendar through your own Google Cloud OAuth client.

Setup (once): Google Cloud console -> APIs & Services -> enable Gmail API and Google Calendar API ->
OAuth consent screen (External, add yourself; set publishing status to "In production" or refresh tokens
expire after 7 days) -> Credentials -> OAuth client ID, type "Desktop app". Paste the client id and secret
into pmjc, then Connect.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from ..connector import Connector, Field, Health, tool
from ..oauth2 import OAuth2Mixin


class Google(OAuth2Mixin, Connector):
    id = "google"
    aliases = ['gmail', 'email', 'mail', 'inbox', 'calendar', 'event', 'meeting', 'schedule', 'draft']  # request words that select this connector (tool_mode: select)
    limitations = "Gmail and Google Calendar only. Never sends email (drafts only); can't delete mail or events."
    native_alternatives = 'a mail/calendar connector in your client that can send or edit.'
    name = "Google (Gmail + Calendar)"
    description = "Search and read Gmail, draft emails, read and add Calendar events."
    docs_url = "https://console.cloud.google.com/apis/credentials"
    credentials = [Field("client_id", "OAuth client ID", secret=False), Field("client_secret", "OAuth client secret"),
                   Field("account", "Sign in", kind="oauth2", required=False)]
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.compose",
              "https://www.googleapis.com/auth/calendar.events"]
    EXTRA_AUTH_PARAMS = {"access_type": "offline", "prompt": "consent"}

    def configured(self) -> bool:
        return super().configured() and self.signed_in()

    async def _get(self, url: str, **params):
        r = await self.http.get(url, params=params, headers={"Authorization": f"Bearer {await self.access_token()}"})
        r.raise_for_status()
        return r.json()

    async def _post(self, url: str, body: dict):
        r = await self.http.post(url, json=body, headers={"Authorization": f"Bearer {await self.access_token()}"})
        r.raise_for_status()
        return r.json()

    async def health(self) -> Health:
        if not self.signed_in():
            return Health(False, "not signed in")
        try:
            await self.access_token()
            return Health(True, "signed in")
        except Exception as e:
            return Health(False, type(e).__name__)

    @tool(scope="read", untrusted_output=True)
    async def gmail_search(self, query: str = "newer_than:1d", max_results: int = 10) -> list[dict]:
        """Search Gmail with Gmail query syntax (e.g. 'from:facebookmail.com newer_than:2d'). Returns sender, subject, snippet."""
        base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        ids = (await self._get(base, q=query, maxResults=min(max_results, 50))).get("messages", [])
        out = []
        for m in ids:
            d = await self._get(f"{base}/{m['id']}", format="metadata", metadataHeaders=["From", "Subject", "Date"])
            h = {x["name"]: x["value"] for x in d["payload"].get("headers", [])}
            out.append({"id": m["id"], "from": h.get("From"), "subject": h.get("Subject"), "date": h.get("Date"),
                        "snippet": d.get("snippet", "")})
        return out

    @tool(scope="read", untrusted_output=True)
    async def gmail_read(self, message_id: str, max_chars: int = 10000) -> dict:
        """Full text of one email. message_id comes from gmail_search."""
        d = await self._get(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}", format="full")

        def walk(p):
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                yield base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "ignore")
            for c in p.get("parts", []) or []:
                yield from walk(c)
        h = {x["name"]: x["value"] for x in d["payload"].get("headers", [])}
        return {"from": h.get("From"), "subject": h.get("Subject"), "date": h.get("Date"),
                "text": "\n".join(walk(d["payload"]))[:max_chars] or d.get("snippet", "")}

    @tool(scope="act", level="auto_clean")
    async def gmail_draft(self, to: str, subject: str, body: str) -> dict:
        """Create a Gmail draft. It is NOT sent: the user sends it from Gmail (follow with pmjc_handoff if they should act now)."""
        msg = EmailMessage()
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        d = await self._post("https://gmail.googleapis.com/gmail/v1/users/me/drafts", {"message": {"raw": raw}})
        return {"draft_id": d["id"]}

    @tool(scope="read", untrusted_output=True)
    async def calendar_upcoming(self, days: int = 7, max_results: int = 25) -> list[dict]:
        """Upcoming events on your primary calendar."""
        now = datetime.now(timezone.utc)
        d = await self._get("https://www.googleapis.com/calendar/v3/calendars/primary/events",
                            timeMin=now.isoformat(), timeMax=(now + timedelta(days=days)).isoformat(),
                            singleEvents="true", orderBy="startTime", maxResults=min(max_results, 100))
        return [{"summary": e.get("summary"), "start": e["start"].get("dateTime", e["start"].get("date")),
                 "end": e["end"].get("dateTime", e["end"].get("date")), "location": e.get("location")}
                for e in d.get("items", [])]

    @tool(scope="act")
    async def calendar_add_event(self, summary: str, start_iso: str, end_iso: str, location: str | None = None,
                                 description: str | None = None) -> dict:
        """Add an event to your primary calendar (ISO 8601 times with timezone offset)."""
        body = {"summary": summary, "start": {"dateTime": start_iso}, "end": {"dateTime": end_iso}}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        e = await self._post("https://www.googleapis.com/calendar/v3/calendars/primary/events", body)
        return {"id": e["id"], "link": e.get("htmlLink")}
