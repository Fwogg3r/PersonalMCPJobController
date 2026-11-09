"""What pmjc can and can't do, task by task, so a session with nothing but pmjc's tools picks the best route.

Each entry: the task, the pmjc tools that do it (with `needs` = connector ids that must be connected), hard limits,
the better route a client may have natively, and the workaround when neither is possible. `pmjc_capabilities`
returns these with `available_now` computed from what's connected.
"""
from __future__ import annotations

CATALOG: list[dict] = [
    {"task": "send a message to someone as the user (Discord, Slack, Messenger, WhatsApp, SMS, iMessage, email)",
     "pmjc": "Not as the user. As a bot: discord_dm / discord_send (Discord bot), slack_send (Slack app), telegram_send "
             "(Telegram bot). Email: gmail_draft creates a draft the user sends.",
     "needs": ["discord", "slack", "telegram", "google"],
     "limits": "Services don't let third-party tools send from a personal account (Discord forbids self-bots; iMessage/SMS "
               "have no API on iPhone). Bot messages show the bot's name. A bot can only DM people who share a server with it.",
     "native": "If your client can act as the user (a messaging connector with the user's own login, or browser/computer "
               "control on a machine where the user is signed in), that is the most natural route; get the user's OK first.",
     "workaround": "pmjc_handoff with contact + service + text: the user gets a notification with a deep link that opens the "
                   "right chat (SMS, WhatsApp and email prefill the text) and sends it with one tap. Add watch_reply=true to catch "
                   "the answer."},
    {"task": "know when someone replies / read messages sent to the user",
     "pmjc": "pmjc_watch_start type=event source=windows_notifications (any desktop app or browser tab that shows "
             "notifications on the pmjc PC), notifications_current; discord_dm_read / discord_read_channel for what the bot can "
             "see; gmail_search for email; telegram_updates for the bot.",
     "needs": ["windows_notifications", "discord", "google", "telegram"],
     "limits": "Notifications carry only the sender's display name and a preview, and only while the app runs and notifies on "
               "the pmjc PC. If the user is active in that app on another device, the PC may not get a notification. "
               "A bot can't read the user's personal DMs.",
     "native": "A client connector with the user's own message access can read full history.",
     "workaround": "Match on the display name the app shows (use pmjc_contact_find for aliases). For full text, browser_read "
                   "on a page pmjc's browser is signed in to (e.g. messenger.com)."},
    {"task": "wait for something, check again later, remind",
     "pmjc": "pmjc_watch_start (time, event or poll) with a thread_id; the result is recorded in the thread and pushed to the "
             "user's notification channel.", "needs": [],
     "limits": "Delivery to the phone needs a notification channel (ntfy, home_assistant, telegram, discord, slack, webhook).",
     "native": "-", "workaround": "Without a channel, results wait in the thread for the next session (pmjc_context shows them)."},
    {"task": "recurring task (every morning, every hour)", "pmjc": "pmjc_job_create", "needs": [],
     "limits": "Runs only while 'Scheduled jobs' is on in the dashboard and a model is available to pmjc.",
     "native": "-", "workaround": "-"},
    {"task": "notify the user / push to their phone",
     "pmjc": "Watch and job results go to notify channels automatically; pmjc_handoff notifies with a link.",
     "needs": ["ntfy", "home_assistant", "telegram", "discord", "slack", "webhook"],
     "limits": "Needs at least one channel connected.", "native": "Your client's own notifications.",
     "workaround": "Tell the user in your reply; record it in the thread."},
    {"task": "email: read / search", "pmjc": "gmail_search, gmail_read", "needs": ["google"],
     "limits": "Gmail only, after the user connects their Google account.", "native": "A mail connector in your client.",
     "workaround": "Email notifications on the PC (windows_notifications) show sender and subject."},
    {"task": "email: send", "pmjc": "gmail_draft (draft only; pmjc never sends email itself)", "needs": ["google"],
     "limits": "The user sends the draft.", "native": "A client mail connector that can send, with the user's OK.",
     "workaround": "gmail_draft, then pmjc_handoff with link https://mail.google.com/mail/u/0/#drafts"},
    {"task": "calendar: read / add events", "pmjc": "calendar_upcoming, calendar_add_event", "needs": ["google"],
     "limits": "Google Calendar only.", "native": "A calendar connector in your client.", "workaround": "-"},
    {"task": "location of the user", "pmjc": "ha_location (Home Assistant companion app)", "needs": ["home_assistant"],
     "limits": "Last known fix only; needs Home Assistant with the phone app.", "native": "Your client's device location.",
     "workaround": "Ask the user, or use a saved note such as 'home city'."},
    {"task": "weather, nearby places, geocoding", "pmjc": "weather_forecast, places_nearby, places_geocode", "needs": [],
     "limits": "OpenStreetMap data: no reviews, ratings or live opening status.", "native": "A maps/search connector.",
     "workaround": "-"},
    {"task": "home devices (lights, switches, scenes)", "pmjc": "ha_states, ha_call_service, or the user's own command tools",
     "needs": ["home_assistant"], "limits": "Protected entities are refused; actions need approval unless auto-approved.",
     "native": "A Home Assistant connector in your client.", "workaround": "-"},
    {"task": "shopping: search eBay listings (price, condition, buying option, quantity)", "pmjc": "ebay_search (up to 10 queries per call)",
     "needs": ["ebay"],
     "limits": "Needs the owner's eBay developer production keyset (App ID + Cert ID; developer.ebay.com, create an app, "
               "copy the Production keyset, then `pmjc auth set ebay`). Listing titles are third-party text, so a run can make "
               "only ONE ebay_search call: put every search of a watch into one `queries` list. Quantity and item specifics "
               "only with details=true (at most 20 lookups per call).",
     "native": "A shopping or browser connector in your client.", "workaround": "-"},
    {"task": "files on the user's machines", "pmjc": "files_list, files_search, files_read (read-only, allowed folders); "
     "ssh_run for other machines", "needs": ["files", "ssh"],
     "limits": "No writing files through files_*; secret-looking files are never readable.",
     "native": "Your client's file tools if it runs on that machine.", "workaround": "ssh_run (approval) to change files."},
    {"task": "web pages that need the user's login", "pmjc": "browser_read (text and links), browser_extract (rows by CSS selector) in pmjc's own signed-in browser, allowed domains",
     "needs": ["browser"], "limits": "Read-only: it can't click, type or submit. After a run has read one page, further reads are refused unless the owner declared the URL or site (read_urls/read_sites) in the job or watch.",
     "native": "Browser or computer control in your client.",
     "workaround": "pmjc_handoff with the link, so the user opens it themselves."},
    {"task": "run a command or script", "pmjc": "ssh_run (other machines over SSH, and the machine pmjc runs on, locally: see ssh_machines), the user's command tools (kind: command YAML)", "needs": ["ssh"],
     "limits": "Needs approval; only configured machines and commands.", "native": "Your client's shell if it has one.",
     "workaround": "Last resort when no tool fits (e.g. editing a file): pmjc_shell_run proposes one short shell command "
                   "for the pmjc host. It always needs the owner's approval, is refused after reading third-party "
                   "content and for callers config doesn't allow, and its output is untrusted."},
    {"task": "use a local model", "pmjc": "lmstudio_status, lmstudio_ask (already-loaded models only)", "needs": ["lmstudio"],
     "limits": "Loading models needs approval.", "native": "-", "workaround": "-"},
    {"task": "GitHub", "pmjc": "github_notifications, github_search_issues, github_comment", "needs": ["github"],
     "limits": "Personal access token scope decides what's visible.", "native": "A GitHub connector in your client.",
     "workaround": "-"},
    {"task": "look at images (Discord attachments, screenshots)", "pmjc": "Tools return image URLs only.", "needs": [],
     "limits": "pmjc doesn't pass images to models yet; Discord attachment URLs expire after about a day.",
     "native": "If your client can fetch and view images, open the URL.", "workaround": "Ask the user to describe or forward it."},
    {"task": "phone calls, SMS/iMessage reading, payments and purchases", "pmjc": "Not possible.", "needs": [],
     "limits": "No safe API exists on the user's devices; purchases are deliberately out of scope.",
     "native": "Only the user, or a client with explicit device control and the user's OK.",
     "workaround": "pmjc_handoff with a tel:/sms: link for the user to act on."},
]


def capabilities(task: str | None = None, connected: set[str] | None = None, tainted: bool = False) -> list[dict]:
    """Catalog entries matching `task`. For a session that has read third-party content (`tainted`), routes that
    leave pmjc's own guard rails (the client's native abilities, links to send) are dropped: text from a message
    must not be able to steer the model toward them."""
    words = [w for w in (task or "").lower().split() if len(w) > 2]
    out = []
    for c in CATALOG:
        hay = (c["task"] + " " + c["pmjc"]).lower()
        if words and not any(w in hay for w in words):
            continue
        e = dict(c)
        if tainted:
            e["native"] = "-"
            e["workaround"] = "-"
            e["note"] = ("this session read third-party content; only pmjc's own tools are suggested. For anything "
                         "else, tell the user and let them continue in a fresh session")
        if connected is not None:
            e["available_now"] = (not c["needs"]) or any(n in connected for n in c["needs"])
            e["connected_for_this"] = [n for n in c["needs"] if n in connected]
        out.append(e)
    return out or [{"task": task, "pmjc": "No catalog entry. Check pmjc_connectors for tools, or tell the user it isn't supported.",
                    "available_now": False}]
