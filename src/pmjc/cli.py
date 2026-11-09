"""pmjc command line. Run `pmjc --help`."""
from __future__ import annotations

import contextlib
import asyncio
import getpass
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="pmjc: connect agents to your services and run jobs over them.", no_args_is_help=True)
token_app = typer.Typer(help="Client tokens (MCP over HTTP, REST, webhooks, dashboard).")
auth_app = typer.Typer(help="Credentials for connectors and model providers.")
jobs_app = typer.Typer(help="Scheduled and event-driven jobs.")
notif_app = typer.Typer(help="Windows notifications: list apps, silence banners.")
svc_app = typer.Typer(help="Run pmjc as an always-on service.")
browser_app = typer.Typer(help="pmjc's signed-in browser profile.")
ssh_app = typer.Typer(help="Machines pmjc can reach over SSH.")
appr_app = typer.Typer(help="Actions waiting for approval.")
door_app = typer.Typer(help="Discord front door: chat with pmjc through your bot.")
tools_app = typer.Typer(help="Inspect the tools offered to models.")
for name, sub in [("token", token_app), ("auth", auth_app), ("jobs", jobs_app), ("notifications", notif_app),
                  ("service", svc_app), ("browser", browser_app), ("ssh", ssh_app), ("approvals", appr_app),
                  ("discord-door", door_app), ("tools", tools_app)]:
    app.add_typer(sub, name=name)


@app.callback()
def main(pmjc_home: Optional[str] = typer.Option(None, help="Data directory (default: per-user app data).")):
    if pmjc_home:
        os.environ["PMJC_HOME"] = pmjc_home


def _out(o) -> None:
    typer.echo(json.dumps(o, indent=2, default=str) if not isinstance(o, str) else o)


# --- run ----------------------------------------------------------------------------------------------
@app.command()
def serve(host: Optional[str] = None, port: Optional[int] = None):
    """Run the HTTP server (dashboard, REST, MCP at /mcp, jobs) in the foreground."""
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    from . import server
    server.run(host, port)


@app.command()
def mcp():
    """Run as an MCP server over stdio (for Claude Desktop, Claude Code, Cursor, ... on this machine)."""
    from . import mcp_server
    mcp_server.build(stdio=True).run(transport="stdio", show_banner=False)


@app.command()
def setup():
    """First run: create the data folder and an admin token, and check what's available."""
    from . import auth, config, creds, memory, registry
    s = config.load()
    creds.store()
    typer.echo(f"data folder: {config.home()}")
    typer.echo(f"model.md (standing rules for every model; edit it in the dashboard): {memory.ensure_model_md()}")
    if not any("admin" in t["scopes"] and not t["revoked"] for t in auth.list_tokens()):
        tok = auth.create_token("dashboard", ["admin"])
        typer.echo(f"\nadmin token (shown once; paste it into the dashboard):\n  {tok}\n")
    reg = registry.get()
    typer.echo(f"connectors: {', '.join(reg.connectors)}")
    if reg.errors:
        typer.echo(f"failed to load: {reg.errors}")
    typer.echo(f"dashboard: http://{s.host}:{s.port}/   (start it with `pmjc serve` or `pmjc service install`)")


# --- tokens -------------------------------------------------------------------------------------------
@token_app.command("create")
def token_create(name: str, scopes: str = typer.Option("sources", help="comma list: sources,actions,jobs,admin"),
                 admin: bool = typer.Option(False, "--admin", help="all scopes (dashboard)")):
    """Create a token. It's shown once."""
    from . import auth
    sc = auth.ALL_SCOPES if admin else [s.strip() for s in scopes.split(",") if s.strip()]
    typer.echo(auth.create_token(name, sc))


@token_app.command("list")
def token_list():
    from . import auth
    _out(auth.list_tokens())


@token_app.command("revoke")
def token_revoke(token_id: Optional[str] = typer.Argument(None), all: bool = typer.Option(False, "--all")):
    """Revoke one token, or --all (kill switch)."""
    from . import auth
    if not token_id and not all:
        raise typer.BadParameter("give a token id or --all")
    _out({"revoked": auth.revoke(None if all else token_id)})


# --- credentials ----------------------------------------------------------------------------------------
@auth_app.command("list")
def auth_list():
    """Connectors and which of their settings are filled in (never the values)."""
    from . import creds, registry
    for c in registry.get().connectors.values():
        fields = [f"{f.name}{'*' if creds.store().has(f'{c.id}.{f.name}') or c.option(f.name) else ''}" for f in c.credentials]
        typer.echo(f"{'OK ' if c.configured() else '-- '} {c.id:22} {', '.join(fields)}")
    typer.echo("\n* = set.  Model provider keys: pmjc auth secret anthropic.api_key | openai.api_key | claude.oauth_token")


@auth_app.command("set")
def auth_set(connector: str):
    """Enter a connector's credentials (secrets are typed hidden)."""
    from . import config, creds, registry
    c = registry.get().connectors.get(connector)
    if not c:
        raise typer.BadParameter(f"unknown connector; try: {', '.join(registry.get().connectors)}")
    if c.description:
        typer.echo(c.description)
    if c.docs_url:
        typer.echo(f"docs: {c.docs_url}")
    s = config.load()
    for f in c.credentials:
        if f.kind == "oauth2":
            typer.echo(f"then sign in with: pmjc auth connect {c.id}")
            continue
        label = f"{f.label or f.name}{' (optional)' if not f.required else ''}{' - ' + f.help if f.help else ''}"
        v = getpass.getpass(label + ": ") if f.secret else typer.prompt(label, default=c.option(f.name, f.default or ""), show_default=True)
        if not v:
            continue
        if f.secret:
            creds.store().set(f"{c.id}.{f.name}", v)
        else:
            s.connectors.setdefault(c.id, {})[f.name] = v
    config.save(s)
    typer.echo(f"{c.id}: {'configured' if c.configured() else 'still missing required fields'}")


@auth_app.command("secret")
def auth_secret(key: str):
    """Store one secret by key, e.g. anthropic.api_key, openai.api_key, claude.oauth_token (from `claude setup-token`)."""
    from . import creds
    creds.store().set(key, getpass.getpass(f"{key}: "))
    typer.echo("stored")


@auth_app.command("clear")
def auth_clear(connector: str):
    from . import creds
    for k in [k for k in creds.store().keys() if k.startswith(connector + ".")]:
        creds.store().delete(k)
    typer.echo("cleared")


@auth_app.command("connect")
def auth_connect(connector: str):
    """OAuth sign-in (e.g. google). The server must be running; open the printed link in your browser."""
    import httpx
    from . import config
    s = config.load()
    tok = os.environ.get("PMJC_ADMIN_TOKEN") or getpass.getpass("admin token: ")
    r = httpx.get(f"http://127.0.0.1:{s.port}/api/connectors/{connector}/oauth", headers={"Authorization": f"Bearer {tok}"})
    r.raise_for_status()
    typer.echo(f"Open this link and sign in:\n{r.json()['url']}")


# --- inspection ---------------------------------------------------------------------------------------
@app.command()
def connectors():
    """Connectors with health."""
    from . import registry

    async def go():
        for c in registry.get().connectors.values():
            try:
                h = await c.health()
                typer.echo(f"{'OK ' if h.ok else '-- '} {c.id:22} {h.detail}")
            except Exception as e:
                typer.echo(f"!! {c.id:22} {type(e).__name__}")
        await registry.get().close()
    asyncio.run(go())


@app.command()
def tools(all: bool = typer.Option(False, "--all", help="include tools of unconfigured connectors")):
    """List tools: name, scope, connector."""
    from . import registry
    for t in registry.get().tools(include_unconfigured=all):
        flags = ",".join(f for f, on in (("egress", t.egress), ("untrusted", t.untrusted_output)) if on)
        typer.echo(f"{t.name:28} {t.scope:5} {t.connector:22} {flags}")


@app.command()
def call(name: str, args: str = typer.Option("{}", help="JSON arguments")):
    """Run a tool yourself (as the owner: act tools run without approval)."""
    from . import auth, executor, registry

    async def go():
        try:
            return await executor.call_tool(auth.admin("cli"), name, json.loads(args))
        finally:
            await registry.get().close()
    _out(asyncio.run(go()))


@app.command()
def ask(text: str, private: bool = typer.Option(False, help="local models only"),
        thread: Optional[str] = typer.Option(None, "--thread", help="continue this thread (t_xxxxxx)")):
    """Ask in plain language: answered now from your connectors, or a watch is started and its answer sent later."""
    from . import ask as ask_mod
    from . import auth, registry

    async def go():
        try:
            return await ask_mod.ask(auth.Caller("ask:cli", "remote", {"sources", "jobs"}), text, private=private,
                                     thread_id=thread)
        finally:
            await registry.get().close()
    _out(asyncio.run(go()))


@tools_app.command("size")
def tools_size(provider: Optional[str] = typer.Option(None, "--provider", help="provider id (shows its schema_mode)")):
    """Size of the tool definitions ask() sends, in full and compact schema mode (~tokens = chars / 4)."""
    from . import agent, ask as ask_mod, auth, config
    prov = None
    if provider:
        prov = next((p for p in config.load().routing if p.id == provider), None)
        if prov is None:
            raise typer.BadParameter(f"no provider {provider}")
    tools = ask_mod.ask_tools(auth.Caller("ask:cli", "remote", {"sources", "jobs"}))
    out = {"tools": len(tools)}
    for mode in ("full", "compact"):
        p = config.ProviderConfig(id="size", kind="openai_compat", schema_mode=mode)
        schema, system = agent.tool_payload(p, tools, "", "openai")
        chars = len(json.dumps(schema, separators=(",", ":"))) + len(system)
        out[mode] = {"chars": chars, "approx_tokens": chars // 4}
    if prov:
        out["provider"] = {"id": prov.id, "schema_mode": prov.schema_mode}
    _out(out)


@app.command()
def watches(all: bool = typer.Option(False, "--all", help="include finished ones")):
    """Temporary monitors started for requests."""
    from .jobs import get_engine
    _out(get_engine().watches.list(include_finished=all))


@app.command("remote")
def remote(public_url: str = typer.Argument(..., help="e.g. https://pmjc.example.com (your tunnel hostname)"),
           allow: str = typer.Option(..., help="email allowed to sign in with OAuth"),
           scopes: str = typer.Option("sources,jobs", help="scopes that identity gets"),
           provider: str = typer.Option("google", help="google | github")):
    """Configure remote access: public URL, allowed host, and OAuth for claude.ai / mobile connectors.
    Then store the OAuth client: pmjc auth secret oauth.client_id / oauth.client_secret, and restart."""
    from urllib.parse import urlparse
    from . import config
    s = config.load()
    url = public_url.rstrip("/")
    host = urlparse(url).hostname
    if not host or not url.startswith("https://"):
        raise typer.BadParameter("use the https:// URL of your tunnel hostname")
    s.public_url = url
    if host not in s.allowed_hosts:
        s.allowed_hosts.append(host)
    s.oauth.enabled = True
    s.oauth.provider = provider
    s.oauth.base_url = url
    s.oauth.allowed[allow.lower()] = [x.strip() for x in scopes.split(",") if x.strip()]
    config.save(s)
    cb = "/auth/callback"
    typer.echo(f"saved. OAuth redirect URI to register with {provider}: {url}{cb}")
    typer.echo(f"MCP URL for claude.ai: {url}/mcp")
    typer.echo(f"REST for phones/Shortcuts: POST {url}/api/ask with a token")


@app.command()
def routing():
    """Model providers in priority order with health and spend."""
    from . import router
    _out(asyncio.run(router.status()))


# --- jobs ---------------------------------------------------------------------------------------------
@jobs_app.command("list")
def jobs_list():
    from . import jobs
    for j in jobs.load_jobs().values():
        typer.echo(f"{'on ' if j.enabled else 'off'} {j.name:28} {json.dumps(j.trigger)}")


@jobs_app.command("run")
def jobs_run(name: str):
    """Run a job once in this process and print the result."""
    from . import jobs, registry

    async def go():
        try:
            e = jobs.get_engine()
            e.jobs = jobs.load_jobs()
            return await e.run_job(name, "cli")
        finally:
            await registry.get().close()
    _out(asyncio.run(go()))


@jobs_app.command("examples")
def jobs_examples(copy: bool = typer.Option(False, "--copy", help="copy them (disabled) into your jobs folder")):
    """Show the example jobs shipped with pmjc."""
    import yaml
    from . import jobs
    src = Path(__file__).resolve().parents[2] / "examples" / "jobs"
    for f in sorted(src.glob("*.yaml")):
        typer.echo(f"- {f.stem}")
        if copy:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            data["enabled"] = False
            dst = jobs.jobs_dir() / f.name
            if not dst.exists():
                dst.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if copy:
        typer.echo(f"copied (disabled) to {jobs.jobs_dir()}; edit them and set enabled: true")


# --- notifications --------------------------------------------------------------------------------------
@notif_app.command("apps")
def notif_apps():
    """Apps that have sent notifications, and whether their banners show."""
    from .connectors import windows_notifications as wn
    for a in wn.list_apps():
        typer.echo(f"{'banner ' if a['banner'] else 'silent '}{'' if a['enabled'] else '(off) '}{a['aumid']}")


@notif_app.command("silence")
def notif_silence(app_id: str):
    """Hide an app's banners but keep its notifications in the notification center for pmjc."""
    from . import config
    from .connectors import windows_notifications as wn
    vals = config.load().connectors.get("windows_notifications", {}).get("silence_values")
    _out(wn.silence(app_id, vals))
    typer.echo("Send yourself a test notification from that app to confirm no banner shows.")


@notif_app.command("unsilence")
def notif_unsilence(app_id: str):
    from .connectors import windows_notifications as wn
    _out(wn.unsilence(app_id))


@notif_app.command("learn")
def notif_learn(app_id: str):
    """Record the exact registry values Windows uses for 'silent' on this PC: toggle the app's banner (and sound)
    off in Settings when asked, and pmjc stores the difference for future `silence` calls."""
    from . import config
    from .connectors import windows_notifications as wn
    before = wn.snapshot(app_id)
    typer.prompt("Now turn OFF banners (and sound) for that app in Settings > System > Notifications, then press Enter",
                 default="", show_default=False)
    after = wn.snapshot(app_id)
    diff = {k: v for k, v in after.items() if before.get(k) != v}
    diff.setdefault("Enabled", 1)
    diff.setdefault("ShowInActionCenter", 1)
    s = config.load()
    s.connectors.setdefault("windows_notifications", {})["silence_values"] = diff
    config.save(s)
    _out({"learned": diff})


# --- browser ------------------------------------------------------------------------------------------
@browser_app.command("login")
def browser_login(url: str):
    """Open pmjc's browser (on your rightmost monitor) to sign in to a site. Close the window when done."""
    from .connectors.browser import Browser
    asyncio.run(Browser().login(url))
    typer.echo("saved")


# --- ssh machines ---------------------------------------------------------------------------------------
@ssh_app.command("add")
def ssh_add(name: str, host: Optional[str] = typer.Option(None, help="address or hostname"),
            user: Optional[str] = None, port: Optional[int] = None,
            key: Optional[str] = typer.Option(None, help="private key file, e.g. ~/.ssh/id_ed25519"),
            jump: Optional[str] = typer.Option(None, help="jump host (ssh -J), e.g. me@bastion.example.com"),
            proxy_command: Optional[str] = typer.Option(None, help='e.g. "cloudflared access ssh --hostname %h"'),
            alias: Optional[str] = typer.Option(None, help="use an existing Host alias from ~/.ssh/config instead")):
    """Add or replace a machine. Key-based login only (pmjc never sends passwords)."""
    from . import config
    from .connectors.local import HOST_NAME
    if not HOST_NAME.match(name):
        raise typer.BadParameter("name: letters, digits, . _ - only")
    if not (host or alias):
        raise typer.BadParameter("give --host or --alias")
    m = {"alias": alias} if alias else {k: v for k, v in dict(host=host, user=user, port=port, key=key, jump=jump,
                                                           proxy_command=proxy_command).items() if v is not None}
    s = config.load()
    opts = s.connectors.setdefault("ssh", {})
    machines = opts.get("machines")
    if machines is None:  # first explicit machine: keep the ~/.ssh/config aliases that were implicitly available
        from .connectors.local import ssh_aliases
        machines = {a: {"alias": a} for a in ssh_aliases()} if opts.get("use_ssh_config", True) else {}
    machines[name] = m
    opts["machines"] = machines
    config.save(s)
    typer.echo(f"saved {name}. Test it: pmjc call ssh_check --args '{{\"machine\": \"{name}\"}}'")


@ssh_app.command("list")
def ssh_list():
    from .connectors.local import Ssh
    _out(asyncio.run(Ssh().ssh_machines()))


@ssh_app.command("remove")
def ssh_remove(name: str):
    from . import config
    s = config.load()
    machines = s.connectors.get("ssh", {}).get("machines") or {}
    typer.echo("removed" if machines.pop(name, None) is not None else "no such machine")
    config.save(s)


# --- approvals ----------------------------------------------------------------------------------------
@appr_app.command("list")
def appr_list():
    from . import db
    _out(db.q("SELECT id,tool,args,requested_by,reason,status,created FROM approvals WHERE status='pending'"))


@appr_app.command("approve")
def appr_approve(approval_id: str, deny: bool = typer.Option(False, "--deny")):
    from . import executor, registry

    async def go():
        try:
            res = await executor.decide(approval_id, not deny, "cli")
            with contextlib.suppress(Exception):
                from . import ask as ask_mod
                await ask_mod.continue_after_approval(approval_id, res)
            return res
        finally:
            await registry.get().close()
    _out(asyncio.run(go()))


# --- discord front door -------------------------------------------------------------------------------
@door_app.command("setup")
def door_setup(owner: list[str] = typer.Option(None, "--owner", help="Your numeric Discord user id (repeatable)."),
               channel: list[str] = typer.Option(None, "--channel", help="Channel id where a message starts a "
                                                 "conversation (repeatable). DMs to the bot always work."),
               enable: bool = typer.Option(True, "--enable/--disable"),
               private: Optional[bool] = typer.Option(None, "--private/--no-private",
                                                      help="Keep these conversations on local models."),
               scopes: Optional[str] = typer.Option(None, help="Comma-separated, default sources,jobs,actions."),
               log_channel: Optional[str] = typer.Option(None, "--log-channel",
                                                         help="Channel id that gets each run's log lines ('' to stop).")):
    """Chat with pmjc through your Discord bot. Restart the service afterwards."""
    from . import config, discord_door
    s = config.load()
    d = s.discord_door
    if log_channel is not None:
        if log_channel and not log_channel.isdigit():
            raise typer.BadParameter(f"{log_channel!r} isn't a numeric Discord channel id")
        d.log_channel_id = log_channel
    for v in (owner or []) + (channel or []):
        if not v.isdigit():
            raise typer.BadParameter(f"{v!r} isn't a numeric Discord id (turn on Developer Mode, then right-click and Copy ID)")
    if owner:
        d.owner_user_ids = list(owner)
    if channel:
        d.channel_ids = list(channel)
    if private is not None:
        d.private = private
    if scopes:
        d.scopes = [x.strip() for x in scopes.split(",") if x.strip()]
    d.enabled = enable
    config.save(s)
    _out({**d.model_dump(), "problems": discord_door.problems(),
          "next": "restart the service, then message the bot (schtasks /End /TN pmjc; schtasks /Run /TN pmjc)"})


@door_app.command("status")
def door_status():
    """Settings and anything missing. Whether it's connected: GET /api/discord-door on the running service."""
    from . import config, discord_door
    _out({**config.load().discord_door.model_dump(), "problems": discord_door.problems()})


# --- service ------------------------------------------------------------------------------------------
@svc_app.command("install")
def svc_install():
    """Start pmjc at logon/boot and restart it if it stops."""
    from . import service
    typer.echo(service.install())


@svc_app.command("uninstall")
def svc_uninstall():
    from . import service
    typer.echo(service.uninstall())


@svc_app.command("status")
def svc_status():
    from . import service
    typer.echo(service.status())


@svc_app.command("run")
def svc_run():
    """Supervisor loop used by the service (restarts the server when it exits)."""
    from . import service
    service.supervise()


@app.command()
def doctor():
    """Check the environment: Python, optional extras, claude CLI, LM Studio, notification access."""
    from . import config, creds, router
    checks = {"python": sys.version.split()[0], "data_folder": str(config.home()),
              "credential_backend": creds.store().name, "claude_cli": bool(shutil.which("claude")),
              "claude_oauth_token": creds.store().has("claude.oauth_token")}
    try:
        import playwright  # noqa
        checks["browser_extra"] = True
    except ImportError:
        checks["browser_extra"] = False
    if sys.platform == "win32":
        from .connectors.windows_notifications import read_listener
        items = asyncio.run(read_listener())
        checks["notification_listener"] = "allowed" if items is not None else "unavailable (database fallback)"
    checks["providers"] = asyncio.run(router.status())
    _out(checks)


if __name__ == "__main__":
    app()
