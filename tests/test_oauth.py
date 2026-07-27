import asyncio
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from renderdesk import tools
from renderdesk.app import app
from renderdesk.auth import MCPAuthMiddleware, get_current_connection_id
from renderdesk.db import session_scope
from renderdesk.models import Connection
from renderdesk.oauth_provider import oauth_provider

from .conftest import make_user

REDIRECT_URI = "https://client.example/callback"


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


def _make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _register_client(client: httpx.AsyncClient, redirect_uri: str = REDIRECT_URI) -> str:
    resp = await client.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["client_id"]


async def _csrf_token(client: httpx.AsyncClient) -> str:
    token = client.cookies.get("csrf_token")
    if token is None:
        await client.get("/dashboard/login")
        token = client.cookies["csrf_token"]
    return token


async def _login(client: httpx.AsyncClient, email: str, password: str) -> None:
    resp = await client.post(
        "/dashboard/login",
        data={
            "email": email,
            "password": password,
            "next": "/dashboard",
            "csrf_token": await _csrf_token(client),
        },
    )
    assert resp.status_code == 303, resp.text


async def _authorize_and_consent(
    client: httpx.AsyncClient, client_id: str, redirect_uri: str, code_challenge: str, state: str = "xyz"
) -> tuple[str, str | None]:
    resp = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
    )
    assert resp.status_code in (302, 303, 307, 308), resp.text
    request_id = parse_qs(urlparse(resp.headers["location"]).query)["request_id"][0]

    resp = await client.post(
        "/oauth/consent",
        data={"request_id": request_id, "action": "approve", "csrf_token": await _csrf_token(client)},
    )
    assert resp.status_code in (302, 303, 307, 308), resp.text
    qs = parse_qs(urlparse(resp.headers["location"]).query)
    assert "code" in qs, resp.headers["location"]
    return qs["code"][0], qs.get("state", [None])[0]


async def _exchange_code(
    client: httpx.AsyncClient, client_id: str, code: str, redirect_uri: str, code_verifier: str
) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )


async def _full_flow(client: httpx.AsyncClient, email: str, password: str) -> tuple[str, dict]:
    await _login(client, email, password)
    client_id = await _register_client(client)
    verifier, challenge = _make_pkce_pair()
    code, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge)
    resp = await _exchange_code(client, client_id, code, REDIRECT_URI, verifier)
    assert resp.status_code == 200, resp.text
    return client_id, resp.json()


async def _dummy_inner_app(scope, receive, send):
    body = f"connection={get_current_connection_id()}".encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


async def _through_mcp_auth_middleware(access_token: str | None) -> tuple[int, bytes]:
    """Exercises the real MCPAuthMiddleware (auth.py) — including the OAuth
    fallback branch this stage added — against a trivial stub inner app,
    rather than standing up FastMCP's full session manager just to prove a
    bearer token resolves to the right connection_id."""
    middleware = MCPAuthMiddleware(_dummy_inner_app)
    headers = [(b"authorization", f"Bearer {access_token}".encode())] if access_token else []
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": headers}
    result: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            result["status"] = message["status"]
        elif message["type"] == "http.response.body":
            result["body"] = message.get("body", b"")

    await middleware(scope, receive, send)
    return result["status"], result.get("body", b"")


async def test_full_pkce_round_trip_authenticates(client):
    await make_user(email="dave1@example.com", password="pw123456")
    _, token_data = await _full_flow(client, "dave1@example.com", "pw123456")

    assert token_data["token_type"] == "Bearer"
    assert token_data["access_token"]
    assert token_data["refresh_token"]

    access_token = await oauth_provider.load_access_token(token_data["access_token"])
    assert access_token is not None

    status, body = await _through_mcp_auth_middleware(token_data["access_token"])
    assert status == 200
    assert body == f"connection={access_token.subject}".encode()


async def test_concurrent_connection_creation_does_not_duplicate(client):
    user_id = await make_user(email="dave-concurrent@example.com", password="pw123456")
    client_id = await _register_client(client)

    results = await asyncio.gather(
        oauth_provider._get_or_create_connection(user_id, client_id),
        oauth_provider._get_or_create_connection(user_id, client_id),
    )
    assert results[0] == results[1]

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Connection).where(Connection.user_id == user_id, Connection.client_id == client_id)
            )
        ).scalars().all()
    assert len(rows) == 1


async def test_wrong_code_verifier_is_rejected(client):
    await make_user(email="dave2@example.com", password="pw123456")
    await _login(client, "dave2@example.com", "pw123456")
    client_id = await _register_client(client)
    _verifier, challenge = _make_pkce_pair()
    code, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge)

    wrong_verifier, _ = _make_pkce_pair()
    resp = await _exchange_code(client, client_id, code, REDIRECT_URI, wrong_verifier)
    assert resp.status_code == 400


async def test_authorization_code_is_single_use(client):
    await make_user(email="dave3@example.com", password="pw123456")
    await _login(client, "dave3@example.com", "pw123456")
    client_id = await _register_client(client)
    verifier, challenge = _make_pkce_pair()
    code, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge)

    first = await _exchange_code(client, client_id, code, REDIRECT_URI, verifier)
    assert first.status_code == 200

    second = await _exchange_code(client, client_id, code, REDIRECT_URI, verifier)
    assert second.status_code == 400


async def test_reused_refresh_token_revokes_connection(client):
    await make_user(email="dave5@example.com", password="pw123456")
    await _login(client, "dave5@example.com", "pw123456")
    client_id = await _register_client(client)
    verifier, challenge = _make_pkce_pair()
    code, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge)
    token_resp = await _exchange_code(client, client_id, code, REDIRECT_URI, verifier)
    assert token_resp.status_code == 200
    token_data = token_resp.json()
    old_refresh = token_data["refresh_token"]

    rotated = await client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh, "client_id": client_id}
    )
    assert rotated.status_code == 200
    new_token_data = rotated.json()
    assert new_token_data["access_token"] != token_data["access_token"]
    assert new_token_data["refresh_token"] != old_refresh

    # Replaying the now-rotated-away token is reuse — must be rejected...
    replay = await client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": old_refresh, "client_id": client_id}
    )
    assert replay.status_code == 400

    # ...and must revoke the whole connection, killing even the legitimately
    # rotated access token.
    status, _ = await _through_mcp_auth_middleware(new_token_data["access_token"])
    assert status == 401


async def test_reauthorizing_same_user_and_client_reuses_connection(client):
    await make_user(email="dave6@example.com", password="pw123456")
    await _login(client, "dave6@example.com", "pw123456")
    client_id = await _register_client(client)

    verifier1, challenge1 = _make_pkce_pair()
    code1, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge1)
    token_data1 = (await _exchange_code(client, client_id, code1, REDIRECT_URI, verifier1)).json()
    connection_1 = await oauth_provider.load_access_token(token_data1["access_token"])

    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_1.subject, "hello", "markdown")

    verifier2, challenge2 = _make_pkce_pair()
    code2, _ = await _authorize_and_consent(client, client_id, REDIRECT_URI, challenge2)
    token_data2 = (await _exchange_code(client, client_id, code2, REDIRECT_URI, verifier2)).json()
    connection_2 = await oauth_provider.load_access_token(token_data2["access_token"])

    assert connection_1.subject == connection_2.subject

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_2.subject, published["artifact_id"])
    assert fetched["artifact_id"] == published["artifact_id"]


async def test_revoking_connection_invalidates_oauth_access_token(client):
    await make_user(email="dave7@example.com", password="pw123456")
    _, token_data = await _full_flow(client, "dave7@example.com", "pw123456")
    access_token = await oauth_provider.load_access_token(token_data["access_token"])

    resp = await client.post(
        f"/dashboard/connections/{access_token.subject}/revoke", data={"csrf_token": await _csrf_token(client)}
    )
    assert resp.status_code == 303

    status, _ = await _through_mcp_auth_middleware(token_data["access_token"])
    assert status == 401


async def test_missing_or_unknown_token_is_unauthorized():
    status, _ = await _through_mcp_auth_middleware(None)
    assert status == 401

    status, _ = await _through_mcp_auth_middleware("not-a-real-token")
    assert status == 401
