"""Paths and user settings. Everything personal lives in PMJC_HOME, never in the repo."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import yaml
from platformdirs import user_data_dir
from pydantic import BaseModel, Field, model_validator


def default_home() -> Path:
    # Windows: not under AppData. Processes started by MSIX-packaged apps (e.g. the Claude desktop app) get
    # AppData writes silently redirected into the package's private LocalCache, so the CLI run from there and
    # the service started by Task Scheduler would see different folders. The user profile isn't virtualized.
    if sys.platform == "win32":
        return Path.home() / ".pmjc"
    return Path(user_data_dir("pmjc", appauthor=False))


def home() -> Path:
    p = Path(os.environ.get("PMJC_HOME") or default_home())
    p.mkdir(parents=True, exist_ok=True)
    return p


def path(*parts: str) -> Path:
    p = home().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class ProviderConfig(BaseModel):
    """One model provider in the routing list."""
    id: str
    kind: str  # openai_compat | anthropic | claude_cli
    enabled: bool = True
    base_url: str | None = None
    model: str | None = None
    credential: str | None = None  # credential-store key holding the API key, e.g. "anthropic.api_key"
    start: str | None = None  # shell command that starts the host if it is down (local or over ssh)
    start_timeout: int = 90
    trust: str = "cloud"  # local | cloud. Jobs marked data: private only use local providers.
    load_model: bool = True  # for LM Studio hosts: run `lms load <model>` if the model isn't loaded
    capabilities: list[str] = Field(default_factory=lambda: ["tools"])
    context_tokens: int = 32000
    schema_mode: str = "full"  # full | compact: compact sends shorter tool descriptions and schemas (small-context models)
    tool_mode: str = "all"  # all | select: select sends core tools in full plus chosen connectors, one-line briefs for the rest
    select_budget_tokens: int | None = None  # tool_mode select: cap (~tokens) on selected connectors' schemas; None: 30% of context_tokens
    max_result_chars: int | None = None  # cap on one tool result sent to the model; None: derived from context_tokens
    condense_at: float = 0.7  # condense older turns when the prompt passes this share of (context_tokens - reserve_tokens)
    reserve_tokens: int = 2048  # room kept for the model's answer
    monthly_budget_usd: float | None = None
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)


def result_cap(p: ProviderConfig) -> int:
    """Chars of one tool result sent to the model: explicit value, else ~4000 up to 32k context, rising to 20000 at 128k."""
    if p.max_result_chars:
        return max(200, p.max_result_chars)
    ctx = p.context_tokens
    if ctx <= 32000:
        return 4000
    return int(min(20000, 4000 + (ctx - 32000) * 16000 / 96000))


def default_providers() -> list[ProviderConfig]:
    lms = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    return [
        ProviderConfig(id="lmstudio-local", kind="openai_compat", base_url="http://127.0.0.1:1234/v1", trust="local",
                       start=f'"{lms}" server start' if lms.exists() else None, capabilities=["tools"]),
        ProviderConfig(id="claude-plan", kind="claude_cli", model="sonnet", context_tokens=200000,
                       capabilities=["tools", "vision"]),
        ProviderConfig(id="anthropic-api", kind="anthropic", model="claude-haiku-5-5", credential="anthropic.api_key",
                       capabilities=["tools", "vision"], context_tokens=200000, monthly_budget_usd=5.0,
                       price_in_per_mtok=1.0, price_out_per_mtok=5.0),
        ProviderConfig(id="openai-api", kind="openai_compat", enabled=False, base_url="https://api.openai.com/v1",
                       model="gpt-5-mini", credential="openai.api_key", capabilities=["tools", "vision"],
                       monthly_budget_usd=5.0),
        ProviderConfig(id="openrouter", kind="openai_compat", enabled=False, base_url="https://openrouter.ai/api/v1",
                       model="anthropic/claude-haiku-5-5", credential="openrouter.api_key",
                       capabilities=["tools", "vision"], monthly_budget_usd=5.0),
    ]


class OAuthConfig(BaseModel):
    """Optional OAuth for remote MCP clients such as claude.ai and the mobile apps."""
    enabled: bool = False
    provider: str = "google"  # google | github
    base_url: str = ""  # public URL of this server, e.g. https://mcp.example.com
    client_id_credential: str = "oauth.client_id"
    client_secret_credential: str = "oauth.client_secret"
    # identity (email or login) -> scopes it receives
    allowed: dict[str, list[str]] = Field(default_factory=dict)


class DiscordDoorConfig(BaseModel):
    """Chat with pmjc through your own Discord bot (see pmjc.discord_door). Off by default."""
    enabled: bool = False
    owner_user_ids: list[str] = Field(default_factory=list)  # only these Discord users are answered
    channel_ids: list[str] = Field(default_factory=list)  # a message here starts a thread; DMs always work
    scopes: list[str] = Field(default_factory=lambda: ["sources", "jobs", "actions"])
    history_turns: int = 6  # earlier request/answer pairs passed back to the model
    private: bool = False  # keep these conversations on local models
    command_name: str = "pmjc"  # the control slash command (/pmjc); rename to taste
    auto_title: bool = True  # rename threads the door starts to a short topic after the first answer
    log_channel_id: str = ""  # every door run's pmjc log lines are posted here
    trace: bool = False  # post one line per tool call in the conversation (per-thread override: /pmjc trace)
    forward_dms: bool = True  # DMs from anyone else (e.g. replies to discord_dm) are forwarded to the owners' DMs
    # and added to the pmjc thread that messaged that person; never acted on


class McpToolOverride(BaseModel):
    """Per-tool changes to the safe defaults of a mounted MCP tool (settings.yaml only)."""
    scope: Literal["read", "act"] | None = None
    untrusted_output: bool | None = None
    egress: bool | None = None


class McpServerConfig(BaseModel):
    """An existing MCP server mounted as a connector (see pmjc.mcp_connector). Edited in settings.yaml only."""
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None  # stdio: argv[0], never run through a shell
    args: list[str] = Field(default_factory=list)
    url: str | None = None  # http: streamable HTTP endpoint
    bearer_credential: str | None = None  # http: credential-store key of a bearer token (default mcp.<id>.bearer)
    env: list[str] = Field(default_factory=list)  # names read from the credential store as mcp.<id>.<NAME>
    enabled: bool = True
    tools: list[str] | None = None  # allowlist of the server's tool names; None = all
    exclude: list[str] = Field(default_factory=list)
    overrides: dict[str, McpToolOverride] = Field(default_factory=dict)  # keyed by the server's tool name
    timeout: float = 20.0  # seconds to connect and list tools
    call_timeout: float = 60.0
    max_output: int = 20000

    @model_validator(mode="after")
    def _check(self):
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"mcp server {self.id}: stdio needs `command`")
        if self.transport == "http" and not (self.url or "").startswith(("http://", "https://")):
            raise ValueError(f"mcp server {self.id}: http needs an http(s) `url`")
        return self


class FrontDoorConfig(BaseModel):
    """OpenAI-compatible endpoint (/v1/models, /v1/chat/completions) for any chat client; see docs/FRONT_DOOR.md."""
    enabled: bool | None = None  # None = on (every request needs a token anyway); false turns the endpoints off
    chat_commands: bool = True  # /help, /tools, /connectors, /approvals, /status, /model, /threads, /new answered by pmjc (no model)
    stream_progress: bool = True  # stream short "_calling tool..._" lines before the answer
    history_turns: int = 6  # earlier request/answer pairs from the pmjc thread passed back to the model
    max_body_bytes: int = 1_000_000
    rate_limit_per_minute: int = 30  # per token
    keepalive_seconds: float = 10.0  # SSE comment interval while waiting (stops mobile clients timing out)


class ResearchConfig(BaseModel):
    """The pmjc_research sub-run (see pmjc.research). Only public-web tools belong in `tools`: anything with
    private output (browser tools, files, notes) is dropped at run time."""
    tools: list[str] = Field(default_factory=lambda: ["web_search", "web_fetch"])
    max_turns: int = 8
    max_chars: int = 2400  # the summary returned to the parent run (about 600 tokens)

class RouteRule(BaseModel):
    """One front-door routing rule: every matcher given must match; the first matching rule wins (see pmjc.routing)."""
    name: str = ""  # label shown in the footer and routing log (default: the prefix, else "rule N")
    tier: str
    prefix: str | None = None  # e.g. "/strong": the request starts with it (then it is stripped and counts as an override)
    keywords: list[str] = Field(default_factory=list)  # any of these as a whole word, case-insensitive
    surface: list[str] | str | None = None  # discord | rest | cli | v1 | job
    min_chars: int = 0
    has_image: bool | None = None  # placeholder: requests carry no images yet


def default_route_rules() -> list[RouteRule]:
    return [RouteRule(prefix="/strong", tier="strong"), RouteRule(prefix="/local", tier="local")]


class RouterConfig(BaseModel):
    """Front-door routing and router tuning. Everything has a generic default that works without configuration."""
    # Process names (substring, case-insensitive) that mean the GPU is in use by the owner, e.g. games or launchers.
    gpu_busy_processes: list[str] = Field(default_factory=list)
    # What to do when the GPU is busy and the plan wanted a guarded local provider (extra.skip_when_gpu_busy_percent).
    # Interactive requests: ask = offer "use local anyway / use a cloud model / wait"; cloud = go to the cloud; local = run anyway.
    gpu_busy_action: str = "ask"
    # Jobs and watches: wait = defer (a job run is recorded as deferred and retried on its next schedule; a watch retries
    # after gpu_busy_backoff_seconds, gpu_busy_max_waits times); cloud / local = go there. Private work never goes to the cloud.
    gpu_busy_jobs: str = "wait"
    gpu_busy_backoff_seconds: float = 120.0
    gpu_busy_max_waits: int = 5
    # Sticky provider after a fallback: stay on the fallback provider when the thread is "hard" (plan tier strong, this
    # many tool calls in the last run, a condense happened, or history over this share of the planned provider's context).
    # Interactive requests (Discord, /v1, REST, CLI) whose remaining candidates are all local (private thread, /local,
    # a local-only tier or model id): when the host or model is down and auto_start_local is on, start it and wait up
    # to this many seconds (a server start plus a 27B model load can take a minute or two) before failing. Never
    # starts anything while the GPU-busy guard says busy, unless the owner chose "use local anyway".
    local_start_wait_seconds: float = 180.0
    # When the plan's FIRST choice is local and down but later candidates are cloud: wait up to this long for the
    # local start before falling back (0 = fall back at once, the local start continues in the background). The
    # fallback is still recorded as a fallback, so the next turn can switch back.
    prefer_local_wait_seconds: float = 0.0
    stay_tool_calls: int = 4
    stay_history_share: float = 0.5
    # Named provider lists; the order is the fallback. Built in (derived from `routing`): default = the routing order,
    # strong = the non-local providers, local = the local providers. Entries here replace or add tiers.
    tiers: dict[str, list[str]] = Field(default_factory=dict)
    route_rules: list[RouteRule] = Field(default_factory=default_route_rules)
    sticky: bool = True  # a thread keeps the provider chosen on its first routed turn
    footer: bool = True  # last line of an answer: provider, rule, tools, cost
    footer_surfaces: list[str] = Field(default_factory=lambda: ["discord", "v1"])


class CommandsConfig(BaseModel):
    """How ssh_run, ssh_check and pmjc_shell_run return (see pmjc.commands)."""
    wait_seconds: int = 8  # a command still running after this many seconds returns a run_id; follow up with command_status


class Settings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8770
    public_url: str = ""  # how phones reach this server (your tunnel), used for approval links
    credential_backend: str = "keyring"  # keyring | file | env
    routing: list[ProviderConfig] = Field(default_factory=default_providers)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)  # per-connector non-secret options
    disabled_connectors: list[str] = Field(default_factory=list)
    enabled_plugins: list[str] = Field(default_factory=list)  # entry-point connector names allowed to load
    auto_approve: list[str] = Field(default_factory=list)  # act tools that run without approval (any caller)
    result_ttl_hours: float = 24.0  # how long a clipped tool result stays pageable with pmjc_result_get
    approval_outcome_notify: bool = True  # notify channels get the follow-up after an approval decided outside Discord
    # Approved shell-command fallback (tool pmjc_shell_run; always needs approval, never from a tainted run).
    # Who may propose: "owner" (chats the owner started: Discord door, REST, CLI, MCP clients), "jobs", "watches".
    # An empty list turns the tool off.
    shell_command_proposers: list[str] = Field(default_factory=lambda: ["owner"])
    shell_command_shell: str = "auto"  # auto (PowerShell on Windows, sh elsewhere) | powershell | cmd | sh
    shell_command_cwd: str = ""  # working directory; empty = the user's home folder
    shell_command_timeout_seconds: int = 30  # default per command; the model may ask for more, up to the maximum
    shell_command_max_timeout_seconds: int = 300
    shell_command_output_cap_chars: int = 10000  # default cap on returned output
    shell_command_max_output_chars: int = 100000
    # Domains (and subdomains) whose links stay clickable in answers and approval previews of runs that read untrusted
    # content; links to any other host are defanged (see pmjc.defang). Links a run's own tools returned are kept too.
    output_link_allowlist: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)  # extra Host headers accepted (e.g. your tunnel hostname)
    # Background activity. Everything is off by default: pmjc only reads data when an agent or you ask.
    monitoring_enabled: bool = False  # run event sources (e.g. polling Windows notifications) in the background
    monitoring_sources: list[str] = Field(default_factory=list)  # connector ids allowed to poll when monitoring is on
    auto_start_local: bool = False  # start local model servers / load models when a run needs them
    scheduled_jobs_enabled: bool = False  # cron/interval/event jobs; manual runs always work
    approval_ttl_seconds: int = 600
    approval_wait_seconds: int = 50  # under the ~60 s tool-call timeout many MCP clients use; an MCP client's act call waits this long for the owner's decision, then returns pending
    approval_poll_seconds: float = 2.0  # how often a waiter checks the database for a decision made in another process
    audit_retention_days: int = 30
    thread_retention_days: int = 90  # closed threads older than this are deleted
    notify_channels: list[str] = Field(default_factory=list)  # connector ids used for approvals/alerts, in order
    # Existing MCP servers mounted as connectors. Only settings.yaml can change this, never the API.
    mcp_servers: list[McpServerConfig] = Field(default_factory=list)
    discord_door: DiscordDoorConfig = Field(default_factory=DiscordDoorConfig)
    front_door: FrontDoorConfig = Field(default_factory=FrontDoorConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)

    router: RouterConfig = Field(default_factory=RouterConfig)
    commands: CommandsConfig = Field(default_factory=CommandsConfig)


_lock = threading.Lock()


def load() -> Settings:
    f = path("settings.yaml")
    if not f.exists():
        s = Settings()
        save(s)
        return s
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)


def save(s: Settings) -> None:
    with _lock:
        f = path("settings.yaml")
        tmp = f.with_suffix(".tmp")
        tmp.write_text(yaml.safe_dump(s.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
        tmp.replace(f)
