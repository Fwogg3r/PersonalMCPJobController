"""Windows notifications as events: whatever desktop apps and browsers show in the notification center.

Reads with the WinRT UserNotificationListener (works from plain Python on Windows 11 once access is allowed;
pmjc asks on first use). Falls back to a read-only copy of wpndatabase.db (undocumented; WAL-aware).

Silencing: `pmjc notifications silence <app>` keeps an app's notifications going to the notification
center (so pmjc sees them) but hides its banners. Settings -> System -> Notifications does the same by hand.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from ..connector import Connector, Event, Health, event_source, tool

log = logging.getLogger("pmjc.notifications")
OTP_HINT = re.compile(r"(?i)\b(code|otp|passcode|verification|verify|2fa|security|one[- ]time|sign[- ]in)\b")
OTP = re.compile(r"\b\d{4,8}\b")
REG = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"


def redact_otp(text: str) -> str:
    return OTP.sub("[code]", text) if OTP_HINT.search(text) else text


async def read_listener() -> list[dict] | None:
    """Current notification-center toasts via WinRT, or None if unavailable/denied."""
    try:
        from winrt.windows.ui.notifications import KnownNotificationBindings, NotificationKinds
        from winrt.windows.ui.notifications.management import UserNotificationListener
    except Exception:
        return None
    lst = UserNotificationListener.current
    status = lst.get_access_status()
    if status != 1:  # 1 = Allowed
        try:
            status = await lst.request_access_async()
        except Exception:
            return None
        if status != 1:
            return None
    out = []
    for n in await lst.get_notifications_async(NotificationKinds.TOAST):
        try:
            b = n.notification.visual.get_binding(KnownNotificationBindings.toast_generic)
            texts = [t.text for t in b.get_text_elements()] if b else []
            out.append({"id": int(n.id), "app": n.app_info.display_info.display_name,
                        "aumid": n.app_info.app_user_model_id, "texts": texts,
                        "ts": n.creation_time.timestamp()})
        except Exception:
            continue
    return out


def read_database() -> list[dict]:
    """Fallback: copy wpndatabase.db (+ -wal/-shm) and parse toast payloads."""
    src = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"
    if not src.exists():
        return []
    with tempfile.TemporaryDirectory() as d:
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(src) + suffix)
            if f.exists():
                shutil.copy2(f, Path(d) / ("wpn.db" + suffix))
        con = sqlite3.connect(Path(d) / "wpn.db")
        try:
            rows = con.execute("SELECT n.Id, n.Payload, n.ArrivalTime, h.PrimaryId FROM Notification n "
                               "JOIN NotificationHandler h ON h.RecordId = n.HandlerId WHERE n.Type='toast'").fetchall()
        except sqlite3.Error as e:
            log.warning("wpndatabase schema not recognized: %s", e)
            return []
        finally:
            con.close()
    out = []
    for nid, payload, arrival, aumid in rows:
        try:
            xml = payload.decode("utf-8", "ignore") if isinstance(payload, bytes) else payload
            texts = [t.text or "" for t in ET.fromstring(xml).iter("text")]
        except Exception:
            texts = []
        ts = (arrival / 1e7 - 11644473600) if arrival else time.time()  # FILETIME -> unix
        out.append({"id": int(nid), "app": aumid.split("!")[-1], "aumid": aumid, "texts": texts, "ts": ts})
    return out


class WindowsNotifications(Connector):
    id = "windows_notifications"
    limitations = 'Only notifications the pmjc PC shows (Windows only): sender name and preview, not full messages; nothing if the app runs only on another device or is silenced.'
    name = "Windows notifications"
    description = ("Notifications from desktop apps and browsers on this PC (Discord, Messenger in Chrome, Outlook, ...). "
                   "Sign in to each service on this PC and allow its notifications.")
    platforms = ["win32"]
    credentials = []

    def __init__(self) -> None:
        super().__init__()
        self._seen: set[int] = set()
        self._primed = False
        self.backend = ""

    def on_poll_start(self) -> None:
        self._primed = False  # first poll after (re)start only records what's already there

    async def _read(self) -> list[dict]:
        items = await read_listener()
        if items is None:
            self.backend = "database"
            return await asyncio.to_thread(read_database)
        self.backend = "listener"
        return items

    def _allowed(self, item: dict) -> bool:
        apps = self.option("apps") or []  # allowlist of display names or AUMIDs; empty = all
        ignore = self.option("ignore_apps") or []
        if apps and item["app"] not in apps and item["aumid"] not in apps:
            return False
        return item["app"] not in ignore and item["aumid"] not in ignore

    def _event(self, item: dict) -> Event:
        texts = [redact_otp(t) for t in item["texts"]] if self.option("redact_otp", True) else item["texts"]
        return Event(source=self.id, kind="toast", title=texts[0] if texts else item["app"],
                     text="\n".join(texts[1:]), ts=item["ts"], key=f"{item['aumid']}:{item['id']}",
                     data={"app": item["app"], "aumid": item["aumid"]})

    @event_source(interval=3)
    async def poll(self):
        items = await self._read()
        new = [i for i in items if i["id"] not in self._seen]
        self._seen = {i["id"] for i in items} | (self._seen if len(self._seen) < 5000 else set())
        if not self._primed:  # don't replay the whole notification center at startup
            self._primed = True
            if not self.option("replay_on_start", False):
                return []
        return [self._event(i) for i in new if self._allowed(i)]

    @tool(scope="read", untrusted_output=True)
    async def notifications_current(self, app: str | None = None, limit: int = 20) -> list[dict]:
        """Notifications currently in the Windows notification center, newest first. Filter by app name."""
        items = sorted([i for i in await self._read() if self._allowed(i)], key=lambda i: -i["ts"])
        if app:
            items = [i for i in items if app.lower() in i["app"].lower() or app.lower() in i["aumid"].lower()]
        return [{"app": e.data["app"], "title": e.title, "text": e.text, "ts": e.ts}
                for e in (self._event(i) for i in items[:limit])]

    @tool(scope="read")
    async def notifications_apps(self) -> list[dict]:
        """Apps that have sent notifications on this PC, with whether their banners are silenced."""
        return list_apps()

    async def health(self) -> Health:
        items = await read_listener()
        if items is not None:
            return Health(True, f"listener allowed, {len(items)} in notification center")
        return Health(bool(read_database()), "listener unavailable; using wpndatabase fallback")


# --- registry helpers (used by the CLI and dashboard; not agent tools) ------------------------------------
def list_apps() -> list[dict]:
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG) as root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            vals = {}
            with winreg.OpenKey(root, name) as k:
                j = 0
                while True:
                    try:
                        n, v, _ = winreg.EnumValue(k, j)
                    except OSError:
                        break
                    j += 1
                    vals[n] = v
            out.append({"aumid": name, "enabled": vals.get("Enabled", 1) != 0,
                        "banner": vals.get("ShowBanner", 1) != 0, "in_center": vals.get("ShowInActionCenter", 1) != 0,
                        "values": {k: v for k, v in vals.items() if isinstance(v, int) and not k.startswith(("Last", "Periodic"))}})
    return out


SILENT = {"Enabled": 1, "ShowInActionCenter": 1, "ShowBanner": 0}


def silence(aumid: str, values: dict[str, int] | None = None) -> dict:
    """Hide banners for one app but keep its notifications in the notification center."""
    import winreg
    vals = values or SILENT
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG + "\\" + aumid, 0, winreg.KEY_SET_VALUE) as k:
        for n, v in vals.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_DWORD, int(v))
    return {"aumid": aumid, "set": vals}


def unsilence(aumid: str) -> dict:
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG + "\\" + aumid, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ShowBanner", 0, winreg.REG_DWORD, 1)
    return {"aumid": aumid, "set": {"ShowBanner": 1}}


def snapshot(aumid: str) -> dict:
    return next((a["values"] for a in list_apps() if a["aumid"] == aumid), {})
