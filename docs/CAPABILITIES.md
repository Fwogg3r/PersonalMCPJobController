# What pmjc can and can't do

Generated from `src/pmjc/capabilities.py` by `scripts/gen_capabilities.py`; edit the catalog, not this file. Sessions get the same data live from `pmjc_capabilities(task)`, which also says what is connected right now.

Pick the **most native route**: your client's own abilities first (if it can act as the user), then pmjc's tools, then pmjc's workarounds.

## Send a message to someone as the user (Discord, Slack, Messenger, WhatsApp, SMS, iMessage, email)

- **pmjc:** Not as the user. As a bot: discord_dm / discord_send (Discord bot), slack_send (Slack app), telegram_send (Telegram bot). Email: gmail_draft creates a draft the user sends.
- **Needs:** discord, slack, telegram, google
- **Limits:** Services don't let third-party tools send from a personal account (Discord forbids self-bots; iMessage/SMS have no API on iPhone). Bot messages show the bot's name. A bot can only DM people who share a server with it.
- **Better if your client can:** If your client can act as the user (a messaging connector with the user's own login, or browser/computer control on a machine where the user is signed in), that is the most natural route; get the user's OK first.
- **Workaround:** pmjc_handoff with contact + service + text: the user gets a notification with a deep link that opens the right chat (SMS, WhatsApp and email prefill the text) and sends it with one tap. Add watch_reply=true to catch the answer.

## Know when someone replies / read messages sent to the user

- **pmjc:** pmjc_watch_start type=event source=windows_notifications (any desktop app or browser tab that shows notifications on the pmjc PC), notifications_current; discord_dm_read / discord_read_channel for what the bot can see; gmail_search for email; telegram_updates for the bot.
- **Needs:** windows_notifications, discord, google, telegram
- **Limits:** Notifications carry only the sender's display name and a preview, and only while the app runs and notifies on the pmjc PC. If the user is active in that app on another device, the PC may not get a notification. A bot can't read the user's personal DMs.
- **Better if your client can:** A client connector with the user's own message access can read full history.
- **Workaround:** Match on the display name the app shows (use pmjc_contact_find for aliases). For full text, browser_read on a page pmjc's browser is signed in to (e.g. messenger.com).

## Wait for something, check again later, remind

- **pmjc:** pmjc_watch_start (time, event or poll) with a thread_id; the result is recorded in the thread and pushed to the user's notification channel.
- **Needs:** nothing (built in)
- **Limits:** Delivery to the phone needs a notification channel (ntfy, home_assistant, telegram, discord, slack, webhook).
- **Better if your client can:** -
- **Workaround:** Without a channel, results wait in the thread for the next session (pmjc_context shows them).

## Recurring task (every morning, every hour)

- **pmjc:** pmjc_job_create
- **Needs:** nothing (built in)
- **Limits:** Runs only while 'Scheduled jobs' is on in the dashboard and a model is available to pmjc.
- **Better if your client can:** -
- **Workaround:** -

## Notify the user / push to their phone

- **pmjc:** Watch and job results go to notify channels automatically; pmjc_handoff notifies with a link.
- **Needs:** ntfy, home_assistant, telegram, discord, slack, webhook
- **Limits:** Needs at least one channel connected.
- **Better if your client can:** Your client's own notifications.
- **Workaround:** Tell the user in your reply; record it in the thread.

## Email: read / search

- **pmjc:** gmail_search, gmail_read
- **Needs:** google
- **Limits:** Gmail only, after the user connects their Google account.
- **Better if your client can:** A mail connector in your client.
- **Workaround:** Email notifications on the PC (windows_notifications) show sender and subject.

## Email: send

- **pmjc:** gmail_draft (draft only; pmjc never sends email itself)
- **Needs:** google
- **Limits:** The user sends the draft.
- **Better if your client can:** A client mail connector that can send, with the user's OK.
- **Workaround:** gmail_draft, then pmjc_handoff with link https://mail.google.com/mail/u/0/#drafts

## Calendar: read / add events

- **pmjc:** calendar_upcoming, calendar_add_event
- **Needs:** google
- **Limits:** Google Calendar only.
- **Better if your client can:** A calendar connector in your client.
- **Workaround:** -

## Location of the user

- **pmjc:** ha_location (Home Assistant companion app)
- **Needs:** home_assistant
- **Limits:** Last known fix only; needs Home Assistant with the phone app.
- **Better if your client can:** Your client's device location.
- **Workaround:** Ask the user, or use a saved note such as 'home city'.

## Weather, nearby places, geocoding

- **pmjc:** weather_forecast, places_nearby, places_geocode
- **Needs:** nothing (built in)
- **Limits:** OpenStreetMap data: no reviews, ratings or live opening status.
- **Better if your client can:** A maps/search connector.
- **Workaround:** -

## Home devices (lights, switches, scenes)

- **pmjc:** ha_states, ha_call_service, or the user's own command tools
- **Needs:** home_assistant
- **Limits:** Protected entities are refused; actions need approval unless auto-approved.
- **Better if your client can:** A Home Assistant connector in your client.
- **Workaround:** -

## Shopping: search eBay listings (price, condition, buying option, quantity)

- **pmjc:** ebay_search (up to 10 queries per call)
- **Needs:** ebay
- **Limits:** Needs the owner's eBay developer production keyset (App ID + Cert ID; developer.ebay.com, create an app, copy the Production keyset, then `pmjc auth set ebay`). Listing titles are third-party text, so a run can make only ONE ebay_search call: put every search of a watch into one `queries` list. Quantity and item specifics only with details=true (at most 20 lookups per call).
- **Better if your client can:** A shopping or browser connector in your client.
- **Workaround:** -

## Multi-page web research: compare sources, follow search results to several pages

- **pmjc:** pmjc_research(question, max_sources)
- **Needs:** web
- **Limits:** Runs a sandboxed sub-run with only web_search and web_fetch (config `research.tools`) and returns a capped summary plus the URLs it read. Call it once per request with a self-contained question of at most 300 characters and no private details: the question goes to the web, and the sub-run knows nothing about the user. The summary is third-party text: afterwards the run can answer but not act freely, and it can't call pmjc_research again. Not available once the run has read web content, so call it before other web tools.
- **Better if your client can:** Your client's own web search and fetch tools.
- **Workaround:** web_search then web_fetch once each.

## Web research: search the web and read ordinary pages

- **pmjc:** web_search (up to 5 queries per call), web_fetch (up to 5 URLs per call, readable text)
- **Needs:** web
- **Limits:** web_search needs a backend the owner configures (self-hosted SearXNG or a Brave Search API key; see docs/CONNECTORS.md). web_fetch is plain HTTP: no JavaScript, no logins, text only (8000 characters per page by default, 2 MB download cap); private, local and blocked domains are refused. Results are third-party text, so a run can make only ONE web_search and ONE web_fetch call after reading web content: put every query or URL into one list. web_fetch of owner-declared read_urls/read_sites stays allowed after taint.
- **Better if your client can:** Your client's own web search and fetch tools.
- **Workaround:** browser_read for pages that need a login or scripts.

## Shopping: search Craigslist posts (price, area, link)

- **pmjc:** craigslist_search (up to 10 queries per call)
- **Needs:** craigslist
- **Limits:** Uses Craigslist's undocumented JSON feed (field layout unverified). Each query needs an area: a numeric area id, or a name the owner mapped in connectors.craigslist.areas. Post titles are third-party text, so a run can make only ONE craigslist_search call: put every search into one `queries` list.
- **Better if your client can:** A shopping or browser connector in your client.
- **Workaround:** browser_read on a Craigslist search page.

## Files on the user's machines

- **pmjc:** files_list, files_search, files_read (read-only, allowed folders); ssh_run for other machines
- **Needs:** files, ssh
- **Limits:** No writing files through files_*; secret-looking files are never readable.
- **Better if your client can:** Your client's file tools if it runs on that machine.
- **Workaround:** ssh_run (approval) to change files.

## Web pages that need the user's login

- **pmjc:** browser_read (text and links), browser_extract (rows by CSS selector), browser_browse (scroll, wait, expand without submitting; read-only), browser_act (type, press keys, click anything; needs approval) in pmjc's own signed-in browser, allowed domains
- **Needs:** browser
- **Limits:** browser_read/extract/browse can't type or submit; browser_act can and needs approval (max 10 steps per call). After a run has read one page, further reads are refused unless the owner declared the URL, prefix or site (read_urls/read_prefixes/read_sites; sites never with a query string) in the job or watch.
- **Better if your client can:** Browser or computer control in your client.
- **Workaround:** pmjc_handoff with the link, so the user opens it themselves.

## Run a command or script

- **pmjc:** ssh_run (other machines over SSH, and the machine pmjc runs on, locally: see ssh_machines), the user's command tools (kind: command YAML)
- **Needs:** ssh
- **Limits:** Needs approval; only configured machines and commands.
- **Better if your client can:** Your client's shell if it has one.
- **Workaround:** Last resort when no tool fits (e.g. editing a file): pmjc_shell_run proposes one short shell command for the pmjc host. It always needs the owner's approval, is refused after reading third-party content and for callers config doesn't allow, and its output is untrusted.

## Use a local model

- **pmjc:** lmstudio_status, lmstudio_ask (already-loaded models only)
- **Needs:** lmstudio
- **Limits:** Loading models needs approval.
- **Better if your client can:** -
- **Workaround:** -

## GitHub

- **pmjc:** github_notifications, github_search_issues, github_comment
- **Needs:** github
- **Limits:** Personal access token scope decides what's visible.
- **Better if your client can:** A GitHub connector in your client.
- **Workaround:** -

## Look at images (Discord attachments, screenshots)

- **pmjc:** Tools return image URLs only.
- **Needs:** nothing (built in)
- **Limits:** pmjc doesn't pass images to models yet; Discord attachment URLs expire after about a day.
- **Better if your client can:** If your client can fetch and view images, open the URL.
- **Workaround:** Ask the user to describe or forward it.

## Phone calls, SMS/iMessage reading, payments and purchases

- **pmjc:** Not possible.
- **Needs:** nothing (built in)
- **Limits:** No safe API exists on the user's devices; purchases are deliberately out of scope.
- **Better if your client can:** Only the user, or a client with explicit device control and the user's OK.
- **Workaround:** pmjc_handoff with a tel:/sms: link for the user to act on.

## Not possible

- Sending a message **as the user** from their own Discord, Slack, Messenger, WhatsApp, SMS or iMessage account: services forbid it or have no API. Use a bot account, or `pmjc_handoff` so the user sends it with one tap.
- Reading the user's personal DMs or full message history: only notification previews on the pmjc PC, or what a bot can see.
- Sending email: pmjc only creates drafts.
- Phone calls, reading SMS/iMessage, payments and purchases.
- Clicking, typing or submitting in a browser: `browser_read` is read-only.
- Writing or deleting files through `files_*`.
- Looking at images: tools return URLs; pmjc doesn't pass images to models yet.
- Delegating a task to another, more capable agent on the user's machine (`pmjc_delegate`): deferred, see the roadmap.
- Anything on a device that isn't the pmjc PC and doesn't show up in a connected service.
