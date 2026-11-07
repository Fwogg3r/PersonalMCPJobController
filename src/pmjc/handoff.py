"""Handoff links: when only the user can do the last step (send as themselves, call, open a chat), pmjc builds a deep link
from a contact's handles and notifies them, so it is one tap for the user and nothing is sent in their name.

Handles are validated and URL-encoded here because they come from notes that agents can propose.
"""
from __future__ import annotations

import re
from urllib.parse import quote

SERVICES = ("discord", "sms", "whatsapp", "telegram", "email", "call")
ALLOWED_SCHEMES = ("https://", "http://", "sms:", "tel:", "mailto:", "tg://")
# What a notification from each app looks like to windows_notifications (matched against app name / AUMID).
APPS = {"discord": ["Discord"], "whatsapp": ["WhatsApp"], "telegram": ["Telegram"],
        "sms": ["Phone Link", "Your Phone", "Messages"], "email": ["Mail", "Outlook", "Gmail"],
        "call": ["Phone Link", "Your Phone"]}

_PHONE = re.compile(r"^\+?[0-9 ().-]{5,24}$")
_EMAIL = re.compile(r"^[^\s@<>\"]+@[^\s@<>\"]+\.[^\s@<>\"]+$")
_USER = re.compile(r"^@?[A-Za-z0-9_]{3,64}$")
_ID = re.compile(r"^[0-9]{5,25}$")


def _phone(h: dict, *keys: str) -> str | None:
    for k in keys:
        v = str(h.get(k, "")).strip()
        if v and _PHONE.match(v):
            return ("+" if v.startswith("+") else "") + re.sub(r"\D", "", v)
    return None


def build_link(service: str, handles: dict, text: str = "", subject: str = "") -> tuple[str | None, str]:
    """Returns (link or None, explanation). The text is URL-encoded; Discord can't prefill, so callers put the text
    in the notification for the user to copy."""
    h = handles or {}
    if service == "discord":
        ch = str(h.get("discord_dm_channel_id", "")).strip()
        if _ID.match(ch):
            return f"https://discord.com/channels/@me/{ch}", "opens the DM; Discord can't prefill, so copy the text from this notification"
        uid = str(h.get("discord_user_id", "")).strip()
        if _ID.match(uid):
            return f"https://discord.com/users/{uid}", "opens their profile (no DM channel id saved); copy the text from this notification"
        return None, "contact has no discord_dm_channel_id or discord_user_id"
    if service in ("sms", "call", "whatsapp"):
        p = _phone(h, "whatsapp_phone", "phone") if service == "whatsapp" else _phone(h, "phone")
        if not p:
            return None, "contact has no valid phone handle"
        if service == "call":
            return f"tel:{p}", "tap to call"
        if service == "sms":
            return f"sms:{p}" + (f"&body={quote(text)}" if text else ""), "opens the messages app with the text filled in"
        return f"https://wa.me/{p.lstrip('+')}" + (f"?text={quote(text)}" if text else ""), "opens WhatsApp with the text filled in"
    if service == "telegram":
        u = str(h.get("telegram_username", "")).strip()
        if _USER.match(u):
            return f"tg://resolve?domain={u.lstrip('@')}", "opens the chat; copy the text from this notification"
        return None, "contact has no telegram_username handle"
    if service == "email":
        e = str(h.get("email", "")).strip()
        if _EMAIL.match(e):
            return f"mailto:{e}?subject={quote(subject)}&body={quote(text)}", "opens a new email with the text filled in"
        return None, "contact has no valid email handle"
    return None, f"service must be one of {SERVICES}"


def safe_link(link: str) -> bool:
    return link.lower().startswith(ALLOWED_SCHEMES) and len(link) <= 2000 and not re.search(r"\s", link)
