import base64
import json
import time

import httpx
import pytest

from app.causely_auth import CauselyAuth


def _jwt(exp: int) -> str:
    def part(data: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
        return raw

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


@pytest.mark.asyncio
async def test_exchanges_client_credentials_and_caches():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"accessToken": "jwt-1", "expiresIn": 3600})

    auth = CauselyAuth(
        "client-id", "client-secret", "https://auth.example/token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await auth.access_token() == "jwt-1"
    assert await auth.access_token() == "jwt-1"
    assert calls == [{"clientId": "client-id", "secret": "client-secret"}]


@pytest.mark.asyncio
async def test_refreshes_when_the_jwt_is_already_expired():
    past = _jwt(int(time.time()) - 10)
    responses = iter([
        httpx.Response(200, json={"access_token": past}),
        httpx.Response(200, json={"token": "fresh", "expiresIn": 3600}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    auth = CauselyAuth(
        "id", "secret", "https://auth.example/token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await auth.access_token() == past
    assert await auth.access_token() == "fresh"


@pytest.mark.asyncio
async def test_missing_credentials_do_not_call_the_token_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("token endpoint should not be called")

    auth = CauselyAuth(
        "", "", "https://auth.example/token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="CAUSELY_CLIENT_ID"):
        await auth.access_token()


@pytest.mark.asyncio
async def test_http_error_does_not_include_the_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    auth = CauselyAuth(
        "id", "super-secret", "https://auth.example/token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="HTTP 401") as exc:
        await auth.access_token()
    assert "super-secret" not in str(exc.value)
