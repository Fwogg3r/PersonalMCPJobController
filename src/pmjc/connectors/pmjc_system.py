"""pmjc's own tools: recent events, jobs, runs, approvals and connector status."""
from __future__ import annotations

import json

from .. import db, executor, registry
from ..connector import Connector, tool


class Pmjc(Connector):
    id = "pmjc"
    name = "pmjc"
    description = "Events, jobs, runs and approvals inside pmjc itself."

    @tool(scope="read", untrusted_output=True)
    async def pmjc_events_recent(self, source: str | None = None, contains: str | None = None, limit: int = 20) -> list[dict]:
        """Recent events from all sources (notifications, webhooks, ...), newest first. Filter by source id or text."""
        sql, args = "SELECT source,kind,title,text,data,ts FROM events WHERE 1=1", []
        if source:
            sql += " AND source=?"; args.append(source)
        if contains:
            sql += " AND (title LIKE ? OR text LIKE ?)"; args += [f"%{contains}%"] * 2
        rows = db.q(sql + " ORDER BY ts DESC LIMIT ?", *args, min(limit, 200))
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    @tool(scope="read")
    async def pmjc_connectors(self) -> list[dict]:
        """Which services are connected and which tools each one offers."""
        return [{"id": c.id, "name": c.name, "configured": c.configured(),
                 "tools": [m["name"] for m, _ in c.tools()]} for c in registry.get().connectors.values()]

    @tool(scope="read")
    async def pmjc_approval_status(self, approval_id: str) -> dict:
        """Status of an action waiting for the user's approval (pending, done, denied, expired, failed)."""
        return executor.approval_status(approval_id)

    # --- watches: temporary monitors for one request ---------------------------------------------------------
    @tool(scope="jobs")
    async def pmjc_watch_start(self, request: str, type: str, then_do: str | None = None,
                               at: str | None = None, after_seconds: int | None = None,
                               source: str | None = None, match_app: list[str] | None = None,
                               match_contains: list[str] | None = None, match_regex: str | None = None,
                               poll_tool: str | None = None, poll_args: dict | None = None, every_seconds: int = 300,
                               condition: str | None = None, tools: list[str] | None = None,
                               expires_in_minutes: int = 1440, private: bool = False, respond: str = "model",
                               _caller=None) -> dict:
        """Start a temporary monitor for ONE request, only when the answer needs waiting. Answer instantly with
        other tools when you can. When the watch fires, pmjc answers the request with what it gathered, sends the
        answer to the user's notification channel, and the watch ends (it also ends at expiry).
        type="time": fire at `at` (ISO 8601) or after `after_seconds`.
        type="event": fire when `source` (e.g. windows_notifications, webhook) emits an event matching match_app /
          match_contains / match_regex. Use for "tell me when X replies".
        type="poll": call read tool `poll_tool` with `poll_args` every `every_seconds` (min 30) and fire when its
          output contains match_contains / matches match_regex, or else when it changes. `condition` adds a yes/no
          question for a local model to confirm.
        request: the user's request in their words. then_do: what to do when it fires, if different.
        private=true keeps the final answer on local models. respond="raw" sends the gathered data without a model.
        Returns the watch id; check it with pmjc_watch_status or wait briefly with pmjc_watch_wait."""
        from ..jobs import get_engine
        match = {k: v for k, v in (("app", match_app), ("contains", match_contains), ("regex", match_regex)) if v}
        spec = {"type": type, "prompt": then_do, "at": at, "after_seconds": after_seconds, "source": source,
                "match": match, "tool": poll_tool, "args": poll_args, "every_seconds": every_seconds,
                "check": condition, "tools": tools, "private": private, "respond": respond}
        spec = {k: v for k, v in spec.items() if v not in (None, {}, [])}
        try:
            return get_engine().watches.start(_caller, request, spec, expires_in_seconds=expires_in_minutes * 60)
        except ValueError as e:
            return {"error": str(e)}

    @tool(scope="jobs")
    async def pmjc_watch_status(self, watch_id: str) -> dict:
        """State of a watch (active, running, done, expired, cancelled, failed) and its result once finished."""
        from ..jobs import get_engine
        return get_engine().watches.status(watch_id)

    @tool(scope="jobs")
    async def pmjc_watch_wait(self, watch_id: str, timeout_seconds: int = 60) -> dict:
        """Wait up to timeout_seconds (max 90) for a watch to finish, then return its status and result."""
        from ..jobs import get_engine
        return await get_engine().watches.wait(watch_id, timeout_seconds)

    @tool(scope="jobs")
    async def pmjc_watch_cancel(self, watch_id: str) -> dict:
        """Stop a watch early."""
        from ..jobs import get_engine
        return get_engine().watches.cancel(watch_id)

    @tool(scope="jobs")
    async def pmjc_watch_list(self, include_finished: bool = False) -> list[dict]:
        """Active watches (or recent ones with include_finished)."""
        from ..jobs import get_engine
        return get_engine().watches.list(include_finished)

    @tool(scope="jobs")
    async def pmjc_jobs_list(self) -> list[dict]:
        """List scheduled and event-driven jobs."""
        from ..jobs import load_jobs
        return [{"name": j.name, "description": j.description, "enabled": j.enabled, "trigger": j.trigger}
                for j in load_jobs().values()]

    @tool(scope="jobs")
    async def pmjc_job_create(self, name: str, prompt: str, cron: str | None = None, interval_seconds: int | None = None,
                              event_source: str | None = None, match_contains: list[str] | None = None,
                              match_app: list[str] | None = None, tools: list[str] | None = None,
                              allow_actions: bool = False, private: bool = False, description: str = "",
                              _caller=None) -> dict:
        """Create or replace a job. Give one trigger: cron ("*/15 * * * *"), interval_seconds, or event_source
        (e.g. "windows_notifications", with optional match_contains / match_app filters). With no trigger the job
        only runs when asked. The prompt should say what to report; answering exactly NOTHING sends no
        notification. private=true keeps the job on local models only."""
        from ..jobs import Job, ModelPref, get_engine, save_job
        trig: dict = {"manual": True}
        if cron:
            trig = {"cron": cron}
        elif interval_seconds:
            trig = {"interval": max(60, interval_seconds)}
        elif event_source:
            trig = {"event": {"source": event_source}}
        match = {}
        if match_contains:
            match["contains"] = match_contains
        if match_app:
            match["app"] = match_app
        job = Job(name=name, prompt=prompt, trigger=trig, match=match, tools=tools, description=description,
                  scopes=["sources", "actions"] if allow_actions else ["sources"], model=ModelPref(private=private))
        f = save_job(job, created_by=_caller)
        get_engine().reload()
        return {"saved": f.name, "trigger": trig}

    @tool(scope="jobs")
    async def pmjc_job_run(self, name: str) -> dict:
        """Run a job now and return its output."""
        from ..jobs import get_engine
        return await get_engine().run_job(name, "manual")

    @tool(scope="jobs")
    async def pmjc_job_delete(self, name: str) -> dict:
        """Delete a job."""
        from ..jobs import delete_job, get_engine
        ok = delete_job(name)
        get_engine().reload()
        return {"deleted": ok}

    @tool(scope="jobs")
    async def pmjc_runs_recent(self, job: str | None = None, limit: int = 10) -> list[dict]:
        """Recent job runs with their output or error."""
        sql, args = "SELECT id,job,trigger,status,provider,started,finished,output,error,cost_usd FROM runs", []
        if job:
            sql += " WHERE job=?"; args.append(job)
        return db.q(sql + " ORDER BY id DESC LIMIT ?", *args, min(limit, 100))
