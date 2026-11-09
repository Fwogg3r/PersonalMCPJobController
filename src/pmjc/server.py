"""HTTP server: MCP (/mcp), REST + OpenAPI (/api, /docs), webhooks (/hooks), approvals, OAuth callbacks, dashboard (/)."""
from __future__ import annotations

import contextlib
import html
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__, agent, auth, config, creds, db, discord_door, executor, jobs, mcp_server, memory, registry, router
from .auth import Caller
from .connector import Event

log = logging.getLogger("pmjc.server")
DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


# --- request guards -------------------------------------------------------------------------------------
_bg: set = set()


def _spawn_continue(aid: str, res: dict) -> None:
    """Continue the approval's thread in the background (phase4/i); the HTTP answer doesn't wait for the model."""
    import asyncio
    from . import ask as ask_mod
    t = asyncio.ensure_future(ask_mod.continue_after_approval(aid, res))
    _bg.add(t)
    t.add_done_callback(_bg.discard)


def _allowed_hosts() -> set[str]:
    s = config.load()
    hosts = {"127.0.0.1", "localhost", "[::1]", *s.allowed_hosts}
    if s.public_url:
        hosts.add(urlparse(s.public_url).hostname or "")
    if s.oauth.base_url:
        hosts.add(urlparse(s.oauth.base_url).hostname or "")
    return {h.lower() for h in hosts if h}


class HostOriginGuard(BaseHTTPMiddleware):
    """Blocks DNS rebinding (Host allowlist) and cross-site requests (Origin must be one of our hosts)."""

    async def dispatch(self, request: Request, call_next):
        hosts = _allowed_hosts()
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host.startswith("[") and "]" in request.headers.get("host", ""):
            host = request.headers["host"].split("]")[0] + "]"
        if host not in hosts:
            return JSONResponse({"error": f"host {host!r} not allowed; add it to allowed_hosts"}, status_code=421)
        origin = request.headers.get("origin")
        if origin and origin != "null" and (urlparse(origin).hostname or "").lower() not in hosts:
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp


def bearer(request: Request) -> Caller:
    h = request.headers.get("authorization", "")
    tok = h[7:] if h.lower().startswith("bearer ") else request.headers.get("x-pmjc-token", "")
    c = auth.verify(tok) if tok else None
    if c is None:
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})
    return c


def need(scope: str):
    def dep(c: Caller = Depends(bearer)) -> Caller:
        if not c.can(scope):
            raise HTTPException(403, f"token lacks scope '{scope}'")
        return c
    return dep


def admin_caller(c: Caller = Depends(need("admin"))) -> Caller:
    return auth.admin(c.name)  # a person's own click in the dashboard


class CredIn(BaseModel):
    values: dict[str, str]

class RoutingIn(BaseModel):
    providers: list[dict[str, Any]]

class Decision(BaseModel):
    approve: bool

class AskIn(BaseModel):
    text: str
    private: bool = False  # local models only
    providers: list[str] | None = None  # provider ids in the order to try
    thread_id: str | None = None  # continue this thread; a new one is created (and returned) if omitted


class AutomateIn(BaseModel):
    text: str
    every_minutes: int | None = None
    daily_at: str | None = None  # "07:30"
    cron: str | None = None
    private: bool = False
    only_when_new: bool = True
    turn_on_scheduler: bool = True  # also switch on "Scheduled and event-triggered jobs"


class JobToggle(BaseModel):
    enabled: bool


class AutoApproveIn(BaseModel):
    tools: list[str]


class GeneralIn(BaseModel):
    monitoring_enabled: bool | None = None
    monitoring_sources: list[str] | None = None
    auto_start_local: bool | None = None
    scheduled_jobs_enabled: bool | None = None


class TokenIn(BaseModel):
    name: str
    scopes: list[str]


# --- app ------------------------------------------------------------------------------------------------
def create_app() -> FastAPI:
    creds.store().load_all_for_redaction()
    for h in logging.getLogger().handlers:
        h.addFilter(creds.Redactor())
    reg = registry.get()
    memory.ensure_model_md()
    mcp = mcp_server.build(stdio=False)
    mcp_app = mcp.http_app(path="/mcp")
    engine = jobs.get_engine()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.lifespan(app):
            engine.start()
            discord_door.start()
            log.info("pmjc %s up with %d connectors, %d tools; data folder %s", __version__, len(reg.connectors), len(reg.tools()), config.home())
            try:
                yield
            finally:
                await discord_door.stop()
                engine.shutdown()
                await registry.get().close()

    app = FastAPI(title="pmjc", version=__version__, lifespan=lifespan,
                  description="Switchboard between agents and your services. Authenticate with `Authorization: Bearer <token>` "
                              "(create one with `pmjc token create`).")
    app.add_middleware(HostOriginGuard)
    app.state.mcp = mcp

    def refresh_tools() -> None:
        registry.reload()
        mcp_server.sync_tools(mcp)

    # ---- dashboard + health
    @app.get("/", include_in_schema=False)
    async def dashboard():
        return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"), headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})

    @app.get("/api/health", tags=["meta"])
    async def health():
        return {"ok": True, "version": __version__, "connectors": len(registry.get().connectors),
                "tools": len(registry.get().tools()), "jobs": len(engine.jobs)}

    @app.get("/api/discord-door", tags=["meta"])
    async def discord_door_status(_: Caller = Depends(admin_caller)):
        """Whether the Discord front door is enabled and connected, and why not."""
        return discord_door.status()

    # ---- tools (any agent framework)
    @app.get("/api/tools", tags=["tools"])
    async def list_tools(c: Caller = Depends(bearer)):
        return [{"name": t.name, "connector": t.connector, "scope": t.scope, "description": t.doc,
                 "egress": t.egress, "untrusted_output": t.untrusted_output, "parameters": registry.tool_json_schema(t)}
                for t in registry.get().tools() if c.can(t.mcp_scope)]

    @app.get("/api/tools/export/{fmt}", tags=["tools"])
    async def export_tools(fmt: str, c: Caller = Depends(bearer)):
        """Tool definitions in OpenAI (`openai`) or Anthropic (`anthropic`) format; execute them with POST /api/tools/{name}."""
        ts = [t for t in registry.get().tools() if c.can(t.mcp_scope)]
        if fmt == "openai":
            return [{"type": "function", "function": {"name": t.name, "description": t.doc[:1000],
                                                      "parameters": registry.tool_json_schema(t)}} for t in ts]
        if fmt == "anthropic":
            return [{"name": t.name, "description": t.doc[:1000], "input_schema": registry.tool_json_schema(t)} for t in ts]
        raise HTTPException(404, "formats: openai, anthropic")

    @app.post("/api/tools/{name}", tags=["tools"])
    async def call(name: str, args: dict[str, Any] | None = None, c: Caller = Depends(bearer)):
        try:
            return {"result": await executor.call_tool(c, name, args or {})}
        except executor.Denied as e:
            raise HTTPException(403, str(e))
        except Exception as e:
            raise HTTPException(502, creds.Redactor.scrub(f"{type(e).__name__}: {e}")[:1000])

    # ---- connectors and credentials (admin)
    @app.get("/api/connectors", tags=["admin"])
    async def connectors(_: Caller = Depends(admin_caller)):
        out = []
        for c in registry.get().connectors.values():
            d = c.describe()
            try:
                h = await c.health()
                d["health"] = {"ok": h.ok, "detail": h.detail}
            except Exception as e:
                d["health"] = {"ok": False, "detail": type(e).__name__}
            d["set"] = [f.name for f in c.credentials if creds.store().has(f"{c.id}.{f.name}") or c.option(f.name)]
            d["oauth"] = hasattr(c, "oauth_start")
            out.append(d)
        return {"connectors": out, "errors": registry.get().errors}


    @app.put("/api/connectors/{cid}/credentials", tags=["admin"])
    async def set_creds(cid: str, body: CredIn, _: Caller = Depends(admin_caller)):
        c = registry.get().connectors.get(cid)
        if not c:
            raise HTTPException(404, "no such connector")
        fields = {f.name: f for f in c.credentials}
        s = config.load()
        for k, v in body.values.items():
            if k not in fields:
                raise HTTPException(400, f"unknown field {k}")
            if fields[k].secret:
                if v:
                    creds.store().set(f"{cid}.{k}", v)
            else:
                s.connectors.setdefault(cid, {})[k] = v
        config.save(s)
        refresh_tools()
        return {"ok": True}

    @app.delete("/api/connectors/{cid}/credentials", tags=["admin"])
    async def clear_creds(cid: str, _: Caller = Depends(admin_caller)):
        for k in [k for k in creds.store().keys() if k.startswith(cid + ".")]:
            creds.store().delete(k)
        refresh_tools()
        return {"ok": True}

    @app.get("/api/connectors/{cid}/oauth", tags=["admin"])
    async def oauth_url(cid: str, request: Request, _: Caller = Depends(admin_caller)):
        c = registry.get().connectors.get(cid)
        if not c or not hasattr(c, "oauth_start"):
            raise HTTPException(404, "not an OAuth connector")
        return {"url": c.oauth_start(f"http://127.0.0.1:{config.load().port}/oauth/{cid}/callback")}

    @app.get("/oauth/{cid}/callback", include_in_schema=False)
    async def oauth_callback(cid: str, state: str = "", code: str = "", error: str = ""):
        c = registry.get().connectors.get(cid)
        if error or not c:
            return HTMLResponse(f"<p>Sign-in failed: {html.escape(error or 'unknown connector')}</p>", 400)
        try:
            await c.oauth_finish(state, code, f"http://127.0.0.1:{config.load().port}/oauth/{cid}/callback")
        except Exception as e:
            return HTMLResponse(f"<p>Sign-in failed: {html.escape(type(e).__name__)}</p>", 400)
        refresh_tools()
        return HTMLResponse(f"<p>{html.escape(c.name)} connected. You can close this tab.</p>")

    # ---- model routing (admin)
    @app.get("/api/routing", tags=["admin"])
    async def routing(_: Caller = Depends(admin_caller)):
        return {"providers": [p.model_dump() for p in config.load().routing], "status": await router.status()}


    @app.put("/api/routing", tags=["admin"])
    async def set_routing(body: RoutingIn, _: Caller = Depends(admin_caller)):
        """Reorder, enable/disable, set models and budgets. `start`/`extra` commands can only be changed in settings.yaml."""
        s = config.load()
        old = {p.id: p for p in s.routing}
        new = []
        for d in body.providers:
            prev = old.get(d.get("id"))
            d = {k: v for k, v in d.items() if k not in ("start", "extra")}
            if prev:
                d = prev.model_dump() | d | {"start": prev.start, "extra": prev.extra}
            new.append(config.ProviderConfig.model_validate(d))
        s.routing = new
        config.save(s)
        return {"ok": True}

    @app.put("/api/secrets/{key}", tags=["admin"])
    async def set_secret(key: str, body: dict[str, str], _: Caller = Depends(admin_caller)):
        """Store a provider secret, e.g. anthropic.api_key or claude.oauth_token."""
        if not key.replace(".", "").replace("_", "").replace("-", "").isalnum():
            raise HTTPException(400, "bad key")
        creds.store().set(key, body["value"])
        return {"ok": True}

    # ---- ask (phones, dashboards, other CLIs) and watches
    @app.post("/api/ask", tags=["ask"])
    async def ask_route(body: AskIn, c: Caller = Depends(need("sources"))):
        """One plain-language request. Answers now from connectors, or starts a temporary watch whose answer is
        sent to your notification channel (and to GET /api/watches/{id}) when it fires."""
        from . import ask as ask_mod
        caller = Caller(f"ask:{c.name}", "remote", set(c.scopes))
        try:
            return await ask_mod.ask(caller, body.text, private=body.private, provider_order=body.providers,
                                     thread_id=body.thread_id)
        except agent.NoProvider as e:
            raise HTTPException(503, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/asks", tags=["ask"])
    async def asks(_: Caller = Depends(need("jobs"))):
        """Recent requests, and the ones asked more than once (candidates to automate)."""
        from . import ask as ask_mod
        return {"recent": ask_mod.recent(), "repeated": ask_mod.repeated()}

    @app.post("/api/asks/automate", tags=["ask"])
    async def automate(body: AutomateIn, c: Caller = Depends(need("jobs"))):
        """Run a request automatically on a schedule instead of asking it again by hand."""
        try:
            job = jobs.job_from_request(body.text, body.every_minutes, body.daily_at, body.cron, body.private, body.only_when_new)
        except ValueError as e:
            raise HTTPException(400, str(e))
        creator = auth.admin(c.name) if c.can("admin") else c
        f = jobs.save_job(job, created_by=creator)
        engine.reload()
        s = config.load()
        if body.turn_on_scheduler and not s.scheduled_jobs_enabled:
            s.scheduled_jobs_enabled = True
            config.save(s)
            engine.apply_settings()
        return {"job": job.name, "file": f.name, "trigger": job.trigger, "scheduler_on": config.load().scheduled_jobs_enabled}

    @app.put("/api/jobs/{name}/enabled", tags=["jobs"])
    async def toggle_job(name: str, body: JobToggle, c: Caller = Depends(need("jobs"))):
        job = jobs.load_jobs().get(name)
        if not job:
            raise HTTPException(404, "no such job")
        job.enabled = body.enabled
        jobs.save_job(job, created_by=auth.admin(c.name) if c.can("admin") else c)
        engine.reload()
        return {"job": name, "enabled": body.enabled}

    @app.get("/api/auto-approve", tags=["admin"])
    async def get_auto_approve(_: Caller = Depends(admin_caller)):
        acts = [{"name": t.name, "connector": t.connector, "doc": t.doc.split(chr(10))[0]}
                for t in registry.get().tools(include_unconfigured=True) if t.scope == "act"]
        return {"auto_approve": config.load().auto_approve, "act_tools": acts}

    @app.put("/api/auto-approve", tags=["admin"])
    async def set_auto_approve(body: AutoApproveIn, _: Caller = Depends(admin_caller)):
        """Actions that run without asking, for clean runs only (never after reading third-party content)."""
        valid = {t.name for t in registry.get().tools(include_unconfigured=True) if t.scope == "act"}
        s = config.load()
        s.auto_approve = sorted(set(body.tools) & valid)
        config.save(s)
        return {"auto_approve": s.auto_approve}

    @app.get("/api/watches", tags=["ask"])
    async def watches_list(all: bool = False, _: Caller = Depends(need("jobs"))):
        return engine.watches.list(include_finished=all)

    @app.get("/api/watches/{wid}", tags=["ask"])
    async def watch_get(wid: str, wait: int = 0, _: Caller = Depends(need("jobs"))):
        """Status and result of a watch; `wait` (seconds, max 90) blocks until it finishes."""
        return await engine.watches.wait(wid, wait) if wait else engine.watches.status(wid)

    @app.delete("/api/watches/{wid}", tags=["ask"])
    async def watch_cancel(wid: str, _: Caller = Depends(need("jobs"))):
        return engine.watches.cancel(wid)

    # ---- background activity switches
    @app.get("/api/settings/general", tags=["admin"])
    async def general(_: Caller = Depends(admin_caller)):
        s = config.load()
        sources = [{"id": c.id, "name": c.name, "configured": c.configured()}
                   for c in registry.get().connectors.values() if c.event_sources()]
        return {"monitoring_enabled": s.monitoring_enabled, "monitoring_sources": s.monitoring_sources,
                "auto_start_local": s.auto_start_local, "scheduled_jobs_enabled": s.scheduled_jobs_enabled,
                "available_sources": sources, "active_polls": [j.id for j in engine.sched.get_jobs() if j.id.startswith("poll:")],
                "stored_events": db.q("SELECT COUNT(*) AS n FROM events")[0]["n"]}

    @app.put("/api/settings/general", tags=["admin"])
    async def set_general(body: GeneralIn, _: Caller = Depends(admin_caller)):
        s = config.load()
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(s, k, v)
        config.save(s)
        return engine.apply_settings()

    @app.delete("/api/events", tags=["admin"])
    async def clear_events(_: Caller = Depends(admin_caller)):
        """Delete every stored event (notifications, webhooks)."""
        return {"deleted": db.x("DELETE FROM events")}

    # ---- jobs
    @app.get("/api/jobs", tags=["jobs"])
    async def list_jobs(_: Caller = Depends(need("jobs"))):
        return [j.model_dump() for j in jobs.load_jobs().values()]

    @app.post("/api/jobs", tags=["jobs"])
    async def put_job(job: jobs.Job, c: Caller = Depends(need("jobs"))):
        creator = auth.admin(c.name) if c.can("admin") else c
        f = jobs.save_job(job, created_by=creator)
        engine.reload()
        return {"saved": f.name}

    @app.delete("/api/jobs/{name}", tags=["jobs"])
    async def del_job(name: str, _: Caller = Depends(need("jobs"))):
        ok = jobs.delete_job(name)
        engine.reload()
        return {"deleted": ok}

    @app.post("/api/jobs/{name}/run", tags=["jobs"])
    async def run_job(name: str, body: dict[str, Any] | None = None, _: Caller = Depends(need("jobs"))):
        extra = json.dumps(body) if body else None
        return await engine.run_job(name, "api", extra=extra)

    @app.get("/api/runs", tags=["jobs"])
    async def runs(limit: int = 50, _: Caller = Depends(need("jobs"))):
        return db.q("SELECT * FROM runs ORDER BY id DESC LIMIT ?", min(limit, 500))

    @app.get("/api/events", tags=["admin"])
    async def events(limit: int = 50, source: str | None = None, _: Caller = Depends(admin_caller)):
        if source:
            return db.q("SELECT * FROM events WHERE source=? ORDER BY ts DESC LIMIT ?", source, min(limit, 500))
        return db.q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", min(limit, 500))

    @app.get("/api/audit", tags=["admin"])
    async def audit_log(limit: int = 100, _: Caller = Depends(admin_caller)):
        return db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", min(limit, 1000))

    # ---- webhooks in (iOS Shortcuts, n8n, Home Assistant, IFTTT, ...)
    @app.post("/hooks/{name}", tags=["webhooks"])
    async def hook(name: str, request: Request, _: Caller = Depends(need("jobs"))):
        """Turn any POST into an event. Jobs with trigger `event: {source: webhook, kind: <name>}` react to it."""
        try:
            body = await request.json()
        except Exception:
            body = {"text": (await request.body()).decode("utf-8", "ignore")[:20000]}
        if not isinstance(body, dict):
            body = {"value": body}
        ev = Event(source="webhook", kind=name, title=str(body.get("title", name))[:300],
                   text=str(body.get("text", body.get("message", "")))[:20000], data=body)
        return {"accepted": await engine.ingest(ev)}

    # ---- context: model.md, threads, notes, contacts (admin only; the dashboard's Context tab)
    @app.get("/api/context/model-md", tags=["admin"])
    async def get_model_md(_: Caller = Depends(admin_caller)):
        return {"text": memory.model_md(), "hash": memory.model_md_hash()}

    @app.put("/api/context/model-md", tags=["admin"])
    async def put_model_md(body: dict[str, str], _: Caller = Depends(admin_caller)):
        try:
            memory.set_model_md(body.get("text", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "hash": memory.model_md_hash()}

    @app.get("/api/context/threads", tags=["admin"])
    async def ctx_threads(include_closed: bool = False, _: Caller = Depends(admin_caller)):
        return memory.threads_recent(limit=200, include_closed=include_closed)

    @app.get("/api/context/threads/{tid}", tags=["admin"])
    async def ctx_thread(tid: str, _: Caller = Depends(admin_caller)):
        t = memory.thread_get(tid)
        if "error" in t:
            raise HTTPException(404, t["error"])
        return t

    @app.post("/api/context/threads/{tid}/close", tags=["admin"])
    async def ctx_thread_close(tid: str, _: Caller = Depends(admin_caller)):
        try:
            return memory.thread_set_status(tid, "closed")
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/context/notes", tags=["admin"])
    async def ctx_notes(query: str = "", _: Caller = Depends(admin_caller)):
        return [n for n in memory.note_search(query, limit=500) if not n["key"].startswith("contact:")]

    @app.put("/api/context/notes", tags=["admin"])
    async def ctx_note_set(body: dict[str, Any], c: Caller = Depends(admin_caller)):
        try:
            tags = [str(t) for t in body.get("tags") or []]
            return memory.note_set(str(body.get("key", "")), str(body.get("value", "")), tags, caller=c)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/context/notes/{key:path}", tags=["admin"])
    async def ctx_note_delete(key: str, c: Caller = Depends(admin_caller)):
        return memory.note_delete(key, caller=c)

    @app.get("/api/context/contacts", tags=["admin"])
    async def ctx_contacts(_: Caller = Depends(admin_caller)):
        return {"active": memory.contact_find(""), "pending": memory.contacts_pending()}

    @app.put("/api/context/contacts", tags=["admin"])
    async def ctx_contact_set(body: dict[str, Any], c: Caller = Depends(admin_caller)):
        try:
            return memory.contact_set(str(body.get("name", "")), body.get("aliases") or [], body.get("handles") or {},
                                      str(body.get("notes", "")), caller=c)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/context/contacts/{name}/{decision}", tags=["admin"])
    async def ctx_contact_decide(name: str, decision: str, _: Caller = Depends(admin_caller)):
        if decision not in ("approve", "reject"):
            raise HTTPException(400, "approve or reject")
        return memory.contact_decide(name, decision == "approve")

    @app.delete("/api/context/contacts/{name}", tags=["admin"])
    async def ctx_contact_delete(name: str, _: Caller = Depends(admin_caller)):
        return {"deleted": memory.contact_delete(name)}

    # ---- approvals
    @app.get("/api/approvals", tags=["admin"])
    async def approvals(status: str = "pending", _: Caller = Depends(admin_caller)):
        return db.q("SELECT id,tool,args,requested_by,reason,status,created,expires,result FROM approvals "
                    "WHERE status=? OR ?='all' ORDER BY created DESC LIMIT 200", status, status)


    @app.post("/api/approvals/{aid}", tags=["admin"])
    async def decide(aid: str, d: Decision, c: Caller = Depends(admin_caller)):
        res = await executor.decide(aid, d.approve, c.name)
        _spawn_continue(aid, res)
        return res

    @app.get("/approve/{aid}", include_in_schema=False)
    async def approve_page(aid: str, sig: str = "", deny: int = 0):
        """Link from a phone notification. GET only shows the request (link previews must not approve)."""
        verb = "deny" if deny else "approve"
        if not auth.check_sig(f"{verb}:{aid}", sig):
            return HTMLResponse("<p>Invalid link.</p>", 403)
        a = executor.approval_status(aid)
        rows = db.q("SELECT tool,args,requested_by,reason FROM approvals WHERE id=?", aid)
        if not rows:
            return HTMLResponse("<p>Unknown request.</p>", 404)
        r = rows[0]
        args = html.escape(json.dumps(json.loads(r["args"]), indent=2))
        button = "" if a["status"] != "pending" else (
            f'<form method="post"><input type="hidden" name="sig" value="{html.escape(sig)}">'
            f'<input type="hidden" name="deny" value="{deny}"><button style="font-size:1.3em;padding:.6em 1.4em">'
            f'{"Deny" if deny else "Approve"}</button></form>')
        return HTMLResponse(f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>pmjc approval</title><body style="font-family:system-ui;max-width:40em;margin:1em auto;padding:0 1em">
<h2>{html.escape(r['tool'])}</h2><p>Requested by {html.escape(r['requested_by'])}. {html.escape(r['reason'] or '')}</p>
<p>Status: <b>{html.escape(a['status'])}</b></p><pre style="white-space:pre-wrap;background:#eee;padding:1em">{args}</pre>{button}""")

    @app.post("/approve/{aid}", include_in_schema=False)
    async def approve_post(aid: str, request: Request):
        form = await request.form()
        deny = str(form.get("deny", "0")) == "1"
        verb = "deny" if deny else "approve"
        if not auth.check_sig(f"{verb}:{aid}", str(form.get("sig", ""))):
            return HTMLResponse("<p>Invalid link.</p>", 403)
        res = await executor.decide(aid, not deny, "approval-link")
        _spawn_continue(aid, res)
        return HTMLResponse(f"<p>{html.escape(res.get('status', ''))}.</p>")

    # ---- tokens and kill switch
    @app.get("/api/tokens", tags=["admin"])
    async def tokens(_: Caller = Depends(admin_caller)):
        return auth.list_tokens()


    @app.post("/api/tokens", tags=["admin"])
    async def new_token(t: TokenIn, _: Caller = Depends(admin_caller)):
        return {"token": auth.create_token(t.name, t.scopes), "note": "shown once"}

    @app.delete("/api/tokens/{tid}", tags=["admin"])
    async def revoke_token(tid: str, _: Caller = Depends(admin_caller)):
        return {"revoked": auth.revoke(tid)}

    @app.post("/api/kill", tags=["admin"])
    async def kill(_: Caller = Depends(admin_caller)):
        """Kill switch: revoke every token (including this one) and pause the scheduler."""
        n = auth.revoke(None)
        engine.sched.pause()
        db.audit("admin", "kill_switch", "", "ok", f"revoked {n}")
        return {"revoked": n, "scheduler": "paused", "next": "create a new admin token with `pmjc token create --admin`"}

    app.mount("/", mcp_app)  # /mcp (+ OAuth discovery routes when enabled); must be last
    return app


def run(host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    s = config.load()
    uvicorn.run(create_app(), host=host or s.host, port=port or s.port, log_level="info", proxy_headers=False)
