"""Reusable OAuth2 (authorization code + PKCE) for connectors that sign in to a user's account.

A connector sets AUTH_URL, TOKEN_URL, SCOPES and has client_id/client_secret fields. The user clicks Connect in
the dashboard (or runs `pmjc auth connect <id>`); pmjc redirects to the provider and back to
http://127.0.0.1:<port>/oauth/<id>/callback, then stores the tokens encrypted and refreshes them as needed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

from . import creds

_pending: dict[str, tuple[str, str, float]] = {}  # state -> (connector id, verifier, created)


class OAuth2Mixin:
    AUTH_URL = ""
    TOKEN_URL = ""
    SCOPES: list[str] = []
    EXTRA_AUTH_PARAMS: dict[str, str] = {}

    def oauth_start(self, redirect_uri: str) -> str:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        _pending[state] = (self.id, verifier, time.time())
        q = {"client_id": self.secret("client_id"), "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(self.SCOPES), "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256", **self.EXTRA_AUTH_PARAMS}
        return self.AUTH_URL + "?" + urlencode(q)

    async def oauth_finish(self, state: str, code: str, redirect_uri: str) -> None:
        cid, verifier, created = _pending.pop(state, (None, None, 0))
        if cid != self.id or time.time() - created > 600:
            raise PermissionError("unknown or expired sign-in attempt; start again")
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.secret("client_id"), "code_verifier": verifier}
        secret = self.secret("client_secret", required=False)
        if secret:
            data["client_secret"] = secret
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(self.TOKEN_URL, data=data, headers={"Accept": "application/json"})
            r.raise_for_status()
            self._save_token(r.json())

    def _save_token(self, tok: dict) -> None:
        old = self._token() or {}
        if "refresh_token" not in tok and "refresh_token" in old:
            tok["refresh_token"] = old["refresh_token"]
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        creds.store().set(f"{self.id}.token", json.dumps(tok))

    def _token(self) -> dict | None:
        raw = creds.store().get(f"{self.id}.token")
        return json.loads(raw) if raw else None

    def signed_in(self) -> bool:
        return creds.store().has(f"{self.id}.token")

    async def access_token(self) -> str:
        tok = self._token()
        if not tok:
            raise PermissionError(f"{self.id}: not signed in. Use Connect in the dashboard or `pmjc auth connect {self.id}`")
        if tok["expires_at"] > time.time():
            return tok["access_token"]
        lock = creds.store().lock(f"{self.id}.token")  # rotating refresh tokens must not refresh twice
        await asyncio.to_thread(lock.acquire)
        try:
            tok = self._token()
            if tok["expires_at"] > time.time():
                return tok["access_token"]
            data = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": self.secret("client_id")}
            secret = self.secret("client_secret", required=False)
            if secret:
                data["client_secret"] = secret
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(self.TOKEN_URL, data=data, headers={"Accept": "application/json"})
                r.raise_for_status()
                self._save_token(r.json())
            return self._token()["access_token"]
        finally:
            lock.release()
