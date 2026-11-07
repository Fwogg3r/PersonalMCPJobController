"""Paths and user settings. Everything personal lives in PMJC_HOME, never in the repo."""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_dir
from pydantic import BaseModel, Field


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
    monthly_budget_usd: float | None = None
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)


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
    trace: bool = False  # post one line per tool call in the conversation (per-thread override: /fwog trace)


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
    allowed_hosts: list[str] = Field(default_factory=list)  # extra Host headers accepted (e.g. your tunnel hostname)
    # Background activity. Everything is off by default: pmjc only reads data when an agent or you ask.
    monitoring_enabled: bool = False  # run event sources (e.g. polling Windows notifications) in the background
    monitoring_sources: list[str] = Field(default_factory=list)  # connector ids allowed to poll when monitoring is on
    auto_start_local: bool = False  # start local model servers / load models when a run needs them
    scheduled_jobs_enabled: bool = False  # cron/interval/event jobs; manual runs always work
    approval_ttl_seconds: int = 600
    audit_retention_days: int = 30
    thread_retention_days: int = 90  # closed threads older than this are deleted
    notify_channels: list[str] = Field(default_factory=list)  # connector ids used for approvals/alerts, in order
    discord_door: DiscordDoorConfig = Field(default_factory=DiscordDoorConfig)


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
