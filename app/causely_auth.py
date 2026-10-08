"""Exchange a Causely MCP client id and secret for a short-lived Frontegg JWT.

Same request as claude-managed-agents: POST {clientId, secret} to the tenant
token endpoint, then send the JWT as Authorization: Bearer. The token is cached
and refreshed shortly before it expires.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx

TOKEN_KEYS = ("access_token", "accessToken", "token")
DEFAULT_LIFETIME_S = 3600
REFRESH_SKEW_S = 60


class CauselyAuth:
    def __init__(self, client_id: str, client_secret: str, token_url: str, *,
                 client: httpx.AsyncClient | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self._client = client
        self._token: str | None = None
        self._issued_at = 0.0
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def access_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and self._still_valid(now):
                return self._token
            token, expires_at = await self._fetch(now)
            self._token = token
            self._issued_at = now
            self._expires_at = expires_at
            return token

    def _still_valid(self, now: float) -> bool:
        lifetime = self._expires_at - self._issued_at
        skew = min(REFRESH_SKEW_S, max(0.0, lifetime / 10))
        return now < self._expires_at - skew

    async def _fetch(self, now: float) -> tuple[str, float]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("CAUSELY_CLIENT_ID and CAUSELY_CLIENT_SECRET are required")
        if not self.token_url:
            raise RuntimeError("CAUSELY_TOKEN_URL is required")
        body = {"clientId": self.client_id, "secret": self.client_secret}
        if self._client is not None:
            resp = await self._client.post(self.token_url, json=body)
        else:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.token_url, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Causely token request failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
        payload = resp.json()
        token = next((payload[k] for k in TOKEN_KEYS if payload.get(k)), None)
        if not token:
            raise RuntimeError(
                "Causely token response had no access token "
                f"(keys: {sorted(payload)})"
            )
        return token, _expires_at(payload, token, now)


def _expires_at(payload: dict, token: str, now: float) -> float:
    for key in ("expiresIn", "expires_in"):
        if payload.get(key):
            return now + int(payload[key])
    if exp := _jwt_exp(token):
        return float(exp)
    return now + DEFAULT_LIFETIME_S


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp else None
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
