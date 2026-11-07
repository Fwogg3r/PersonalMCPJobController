"""Slash commands for the Discord front door (/tool and /fwog). Owner only; registered as guild commands.

Every command is acknowledged at once (deferred) and answered by editing the original reply, because tools and jobs
can take longer than Discord's 3 second limit. `/tool` bypasses the model but not approvals.
"""
from __future__ import annotations

import json
import logging

from . import config, executor, memory, registry, router
from .auth import Caller

log = logging.getLogger("pmjc.discord_commands")

EPHEMERAL = 64
SWITCHES = {"monitoring": "monitoring_enabled", "scheduled_jobs": "scheduled_jobs_enabled",
            "auto_start_local": "auto_start_local"}
ONOFF = [{"name": "on", "value": "on"}, {"name": "off", "value": "off"}]


def _s(name, desc, **kw):
    return {"type": 3, "name": name, "description": desc, **kw}


def _sub(name, desc, *opts):
    return {"type": 1, "name": name, "description": desc, "options": list(opts)}


def _grp(name, desc, *subs):
    return {"type": 2, "name": name, "description": desc, "options": list(subs)}


def definitions() -> list[dict]:
    """The application commands to register."""
    onoff = _s("state", "on or off", required=True, choices=ONOFF)
    return [
        {"type": 1, "name": "tool", "description": "Run a pmjc tool directly (no model; act tools still need approval)",
         "options": [_s("name", "Tool name", required=True, autocomplete=True),
                     _s("args", "JSON object of arguments")]},
        {"type": 1, "name": "fwog", "description": "Control pmjc", "options": [
            _sub("new", "Start a new thread", _s("title", "Thread title", required=True)),
            _sub("close", "Archive this thread and close its pmjc thread"),
            _sub("delete", "Delete this thread (asks first)"),
            _sub("trace", "Show tool calls in this thread", onoff),
            _sub("model", "Pick the model provider for this thread",
                 _s("provider", "auto or a provider id", required=True, autocomplete=True)),
            _sub("private", "Keep this thread on local models", onoff),
            _grp("autoapprove", "Act tools that run without a button",
                 _sub("add", "Allow a tool without approval", _s("tool", "Act tool", required=True, autocomplete=True)),
                 _sub("remove", "Require approval again", _s("tool", "Act tool", required=True, autocomplete=True)),
                 _sub("list", "Show auto-approved tools")),
            _grp("jobs", "Jobs",
                 _sub("list", "List jobs"),
                 _sub("run", "Run a job now", _s("name", "Job", required=True, autocomplete=True)),
                 _sub("enable", "Enable a job", _s("name", "Job", required=True, autocomplete=True)),
                 _sub("disable", "Disable a job", _s("name", "Job", required=True, autocomplete=True))),
            _grp("watches", "Watches",
                 _sub("list", "List active watches"),
                 _sub("cancel", "Cancel a watch", _s("id", "Watch", required=True, autocomplete=True))),
            _sub("notify", "Send pmjc notifications to this channel", _s("where", "Use 'here'", choices=[
                {"name": "here", "value": "here"}])),
            _sub("switch", "Turn a background feature on or off",
                 _s("name", "Feature", required=True, choices=[{"name": n, "value": n} for n in SWITCHES]), onoff),
            _grp("friend", "People pmjc may message",
                 _sub("add", "Remember a Discord user", _s("name", "Name", required=True),
                      _s("user_id", "Discord user id", required=True)),
                 _sub("list", "List contacts")),
            _sub("status", "Door, providers and pending approvals"),
        ]},
    ]


def flatten(options: list[dict] | None) -> tuple[list[str], dict, str | None]:
    """(subcommand path, option values, focused option name) from an interaction's nested options."""
    path, vals, focus = [], {}, None
    while options:
        o = options[0]
        if o.get("type") in (1, 2):
            path.append(o["name"])
            options = o.get("options") or []
            continue
        for o in options:
            vals[o["name"]] = o.get("value")
            if o.get("focused"):
                focus = o["name"]
        break
    return path, vals, focus


def clip(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def codeblocks(text: str, size: int = 1900) -> list[str]:
    return [f"```\n{text[i:i + size]}\n```" for i in range(0, max(len(text), 1), size)]


class Commands:
    def __init__(self, door):
        self.door = door

    # ---- registration
    async def register(self) -> None:
        app = self.door.app_id
        guilds = set()
        for ch in self.door.cfg.channel_ids:
            g = await self.door.guild_of(ch)
            if g:
                guilds.add(g)
        for g in guilds:
            await self.door.api("PUT", f"/applications/{app}/guilds/{g}/commands", definitions())
        log.info("discord door: commands registered in %d server(s)", len(guilds))

    # ---- entry point
    async def handle(self, i: dict) -> None:
        user = (i.get("member") or {}).get("user") or i.get("user") or {}
        cb = f"/interactions/{i['id']}/{i['token']}/callback"
        auto = i.get("type") == 4
        if not self.door.is_owner(user.get("id")):
            body = ({"type": 8, "data": {"choices": []}} if auto else
                    {"type": 4, "data": {"content": "Only the owner can use this.", "flags": EPHEMERAL}})
            await self.door.api("POST", cb, body)
            return
        data = i.get("data") or {}
        path, vals, focus = flatten(data.get("options"))
        if auto:
            choices = await self.complete(data.get("name"), path, focus, str(vals.get(focus, "")).lower())
            await self.door.api("POST", cb, {"type": 8, "data": {"choices": choices[:25]}})
            return
        fast_public = data.get("name") == "tool" or path[:1] in (["new"], ["close"], ["delete"])
        await self.door.api("POST", cb, {"type": 5, "data": {} if fast_public else {"flags": EPHEMERAL}})
        ch = i.get("channel_id", "")
        try:
            out = await (self.tool(user["id"], ch, vals) if data.get("name") == "tool"
                         else self.fwog(user["id"], ch, path, vals))
        except Exception as e:
            log.exception("slash command failed")
            out = f"That failed: {type(e).__name__}: {str(e)[:300]}"
        await self.reply(i, out)

    async def reply(self, i: dict, out) -> None:
        text, comps = (out, None) if isinstance(out, (str, list)) else out
        parts = text if isinstance(text, list) else [text or "(done)"]
        app = i.get("application_id") or self.door.bot_id
        base = f"/webhooks/{app}/{i['token']}"
        mentions = {"parse": []}
        first = {"content": parts[0][:2000], "allowed_mentions": mentions, "components": comps or []}
        await self.door.api("PATCH", f"{base}/messages/@original", first)
        for p in parts[1:]:
            await self.door.api("POST", base, {"content": p[:2000], "allowed_mentions": mentions})

    # ---- autocomplete
    async def complete(self, cmd: str, path: list[str], focus: str | None, q: str) -> list[dict]:
        def pick(names, labels=None):
            return [{"name": (labels or {}).get(n, n)[:100], "value": n[:100]} for n in names if q in n.lower()
                    or q in (labels or {}).get(n, "").lower()]
        if cmd == "tool":
            return pick(sorted(t.name for t in self.allowed_tools()))
        if path == ["model"]:
            return pick(["auto"] + [p.id for p in config.load().routing if p.enabled])
        if path[:1] == ["autoapprove"]:
            return pick(sorted(t.name for t in registry.get().tools() if t.scope == "act"))
        if path[:1] == ["jobs"]:
            from . import jobs
            return pick(sorted(jobs.load_jobs()))
        if path == ["watches", "cancel"]:
            from . import jobs
            ws = jobs.get_engine().watches.list()
            return pick([w["id"] for w in ws], {w["id"]: f"{w['id']} {clip(w['request'], 60)}" for w in ws})
        return []

    # ---- /tool
    def caller(self, user_id: str) -> Caller:
        return Caller(f"discord:{user_id}", "remote", set(self.door.cfg.scopes))

    def allowed_tools(self):
        c = self.caller("0")
        return [t for t in registry.get().tools() if c.can(t.mcp_scope)]

    async def tool(self, user_id: str, ch: str, vals: dict):
        name = vals.get("name", "")
        try:
            args = json.loads(vals.get("args") or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return "`args` must be a JSON object, e.g. {\"query\": \"x\"}."
        c = self.caller(user_id)
        c.thread_id = self.door.thread_for(ch)
        try:
            res = await executor.call_tool(c, name, args)
        except executor.Denied as e:
            return f"Denied: {e}"
        except Exception as e:
            return f"`{name}` failed: {type(e).__name__}: {str(e)[:500]}"
        if isinstance(res, dict) and res.get("status") == "pending_approval":
            from . import db, discord_door as dd
            a = db.q("SELECT id, tool, args, reason FROM approvals WHERE id=?", res["approval_id"])
            if a:
                return dd.approval_text(a[0]), dd.approval_buttons(a[0]["id"])
        text = res if isinstance(res, str) else json.dumps(res, indent=2, default=str)
        return codeblocks(text)

    # ---- /fwog
    async def fwog(self, user_id: str, ch: str, path: list[str], v: dict):
        d = self.door
        head = path[0] if path else ""
        sub = path[1] if len(path) > 1 else ""
        if head == "new":
            return await self.new_thread(ch, v["title"])
        if head in ("close", "delete"):
            tid = d.thread_for(ch)
            if not await d.in_door_thread(ch):
                return "Use this inside a thread under a door channel."
            if head == "delete":
                return (f"Delete this thread{' and close its pmjc thread' if tid else ''}? This can't be undone.",
                        [{"type": 1, "components": [
                            {"type": 2, "style": 4, "label": "Delete", "custom_id": f"pmjc:del:{ch}"},
                            {"type": 2, "style": 2, "label": "Cancel", "custom_id": f"pmjc:keep:{ch}"}]}])
            await d.api("PATCH", f"/channels/{ch}", {"archived": True, "locked": True})
            await d.close_thread(ch)
            return "Closed."
        if head == "trace":
            d.set_pref(ch, "trace", v["state"] == "on")
            return f"Tool trace {v['state']} for this thread."
        if head == "private":
            d.set_pref(ch, "private", v["state"] == "on")
            return f"Private (local models only) {v['state']} for this thread."
        if head == "model":
            p = v["provider"]
            ids = [x.id for x in config.load().routing if x.enabled]
            if p != "auto" and p not in ids:
                return f"No enabled provider `{p}`. Options: auto, {', '.join(ids)}."
            d.set_pref(ch, "provider", None if p == "auto" else p)
            return f"Model for this thread: {p}."
        if head == "autoapprove":
            return self.autoapprove(sub, v.get("tool", ""))
        if head == "jobs":
            return await self.jobs_cmd(sub, v.get("name", ""), user_id)
        if head == "watches":
            return self.watches_cmd(sub, v.get("id", ""))
        if head == "notify":
            return await self.notify_here(ch)
        if head == "switch":
            return self.switch(v["name"], v["state"] == "on")
        if head == "friend":
            if sub == "add":
                c = memory.contact_set(v["name"], handles={"discord_user_id": str(v["user_id"])})
                return f"Saved {c['name']} (discord user {v['user_id']}), trusted as the owner's contact."
            rows = memory.contact_find("")
            return "\n".join(f"{r['name']}: {r.get('handles', {}).get('discord_user_id', '-')}" for r in rows) or "No contacts."
        if head == "status":
            return await self.status()
        return "Unknown command."

    async def new_thread(self, ch: str, title: str) -> str:
        d = self.door
        door_ch = ch if ch in d.cfg.channel_ids else await d.parent_of(ch)
        if door_ch not in d.cfg.channel_ids:
            return "Use this in a door channel."
        t = await d.api("POST", f"/channels/{door_ch}/threads", {"name": title[:90], "type": 11, "auto_archive_duration": 1440})
        tid = memory.thread_new(title, by="discord", private=d.cfg.private, trust="owner")
        d.parents[t["id"]] = door_ch
        d.bind(t["id"], tid)
        await d.send(t["id"], f"Hello. This thread is `{tid}`. Write here to talk to pmjc.")
        return f"Started <#{t['id']}>."

    def autoapprove(self, sub: str, tool: str) -> str:
        s = config.load()
        if sub == "list":
            return ("Auto-approved: " + ", ".join(sorted(s.auto_approve))) if s.auto_approve else "No tools are auto-approved."
        t = registry.get().tool(tool)
        if sub == "add":
            if t is None or t.scope != "act":
                return f"`{tool}` isn't an act tool."
            if tool not in s.auto_approve:
                s.auto_approve.append(tool)
                config.save(s)
            return f"Warning: `{tool}` will now run without a button, for every caller."
        if tool in s.auto_approve:
            s.auto_approve.remove(tool)
            config.save(s)
        return f"`{tool}` needs approval again."

    async def jobs_cmd(self, sub: str, name: str, user_id: str) -> str:
        from . import jobs
        eng = jobs.get_engine()
        if sub == "list":
            js = jobs.load_jobs()
            return "\n".join(f"{'on ' if j.enabled else 'off'} {n}: {clip(j.description or j.prompt, 70)}"
                             for n, j in sorted(js.items())) or "No jobs."
        if sub == "run":
            res = await eng.run_job(name)
            return codeblocks(json.dumps(res, indent=2, default=str)[:3800])
        job = jobs.load_jobs().get(name)
        if not job:
            return f"No job `{name}`."
        job.enabled = sub == "enable"
        jobs.save_job(job, created_by=self.caller(user_id))
        eng.reload()
        return f"Job `{name}` {'enabled' if job.enabled else 'disabled'}."

    def watches_cmd(self, sub: str, wid: str) -> str:
        from . import jobs
        w = jobs.get_engine().watches
        if sub == "cancel":
            return f"Watch `{wid}`: {w.cancel(wid).get('status', '?')}."
        rows = w.list()
        return "\n".join(f"`{r['id']}` {r['status']}: {clip(r['request'], 80)}" for r in rows) or "No active watches."

    async def notify_here(self, ch: str) -> str:
        d = self.door
        target = ch if ch in d.cfg.channel_ids or not await d.in_door_thread(ch) else await d.parent_of(ch)
        s = config.load()
        s.connectors.setdefault("discord", {})["notify_channel_id"] = target
        s.notify_channels = ["discord"] + [c for c in s.notify_channels if c != "discord"]
        config.save(s)
        return f"Notifications now go to <#{target}> first."

    def switch(self, name: str, on: bool) -> str:
        s = config.load()
        setattr(s, SWITCHES[name], on)
        config.save(s)
        if name in ("monitoring", "scheduled_jobs"):
            try:
                from . import jobs
                jobs.get_engine().apply_settings()
            except Exception:
                log.warning("apply_settings failed", exc_info=True)
        return f"{name} is {'on' if on else 'off'}."

    async def status(self) -> str:
        from . import db, discord_door as dd
        st = dd.status()
        prov = await router.status()
        pending = db.q("SELECT COUNT(*) n FROM approvals WHERE status='pending'")[0]["n"]
        lines = [f"Door: {'connected' if st['connected'] else 'offline'}, {st['conversations']} conversation(s), "
                 f"{pending} pending approval(s)."]
        for p in prov:
            lines.append(f"{'ok ' if p['healthy'] else ('off' if not p['enabled'] else 'down')} {p['id']} "
                         f"({p['kind']}, {p['trust']}) ${p['month_spend_usd']}")
        return "\n".join(lines)

