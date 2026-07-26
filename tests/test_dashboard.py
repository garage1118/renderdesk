import re

import httpx
import pytest
from sqlalchemy import select

from renderdesk import auth, comments, shares, tools
from renderdesk.app import app
from renderdesk.db import session_scope
from renderdesk.models import Connection

from .conftest import make_connection, make_user


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


async def _login(client, email, password):
    return await client.post("/dashboard/login", data={"email": email, "password": password})


async def test_unauthenticated_dashboard_redirects_to_login(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login?next=%2Fdashboard"


async def test_wrong_password_rejected(client):
    await make_user(email="dave@example.com", password="correct-horse")

    resp = await _login(client, "dave@example.com", "wrong-password")

    assert resp.status_code == 401
    assert "renderdesk_session" not in client.cookies


async def test_login_then_dashboard_lists_own_artifact(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")

    login_resp = await _login(client, "dave@example.com", "correct-horse")
    assert login_resp.status_code == 303
    assert "renderdesk_session" in client.cookies

    dashboard_resp = await client.get("/dashboard")
    assert dashboard_resp.status_code == 200
    assert published["artifact_id"] in dashboard_resp.text


async def test_dashboard_does_not_show_another_users_artifact(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    other_connection = await make_connection()  # belongs to a different, auto-created user
    async with session_scope() as session:
        other_published = await tools.publish_artifact(session, other_connection, "not yours", "markdown")

    await _login(client, "dave@example.com", "correct-horse")

    dashboard_resp = await client.get("/dashboard")
    assert other_published["artifact_id"] not in dashboard_resp.text

    detail_resp = await client.get(f"/dashboard/a/{other_published['artifact_id']}")
    assert detail_resp.status_code == 404


async def test_comment_posted_via_dashboard_is_visible_to_mcp_and_vice_versa(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await _login(client, "dave@example.com", "correct-horse")

    comment_resp = await client.post(f"/dashboard/a/{artifact_id}/comments", data={"body": "please fix the title"})
    assert comment_resp.status_code == 303

    async with session_scope() as session:
        threads = await comments.list_comments(session, connection_id, artifact_id)
    assert len(threads) == 1
    assert threads[0]["comments"][0] == {
        "comment_id": threads[0]["comments"][0]["comment_id"],
        "body": "please fix the title",
        "author": "human",
        "created_at": threads[0]["comments"][0]["created_at"],
    }
    thread_id = threads[0]["thread_id"]

    async with session_scope() as session:
        await comments.reply_to_comment(session, connection_id, thread_id, "fixed!")

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}")
    assert "fixed!" in detail_resp.text


async def test_shared_artifact_shows_up_for_recipient_but_not_via_their_mcp_connection(client):
    owner_id = await make_user(email="owner@example.com", password="owner-pass")
    owner_connection = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, owner_connection, "hello", "markdown")
    artifact_id = published["artifact_id"]

    recipient_id = await make_user(email="recipient@example.com", password="recipient-pass")
    recipient_connection = await make_connection(user_id=recipient_id)

    async with session_scope() as session:
        await shares.share_artifact(session, owner_connection, artifact_id, "recipient@example.com")

    await _login(client, "recipient@example.com", "recipient-pass")

    dashboard_resp = await client.get("/dashboard")
    assert dashboard_resp.status_code == 200
    assert "No artifacts published yet." in dashboard_resp.text  # recipient owns nothing themselves
    assert artifact_id in dashboard_resp.text  # but sees it under "Shared with you"

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}")
    assert detail_resp.status_code == 200

    comment_resp = await client.post(f"/dashboard/a/{artifact_id}/comments", data={"body": "looks great"})
    assert comment_resp.status_code == 303

    # Sharing is dashboard-only — the recipient's own MCP connection gets no
    # extra access to this artifact.
    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, recipient_connection, artifact_id)
    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await comments.list_comments(session, recipient_connection, artifact_id)


async def test_login_bounces_back_to_next_param(client):
    await make_user(email="dave@example.com", password="correct-horse")

    resp = await client.get("/dashboard/a/does-not-exist")
    assert resp.status_code == 303
    login_url = resp.headers["location"]
    assert login_url == "/dashboard/login?next=%2Fdashboard%2Fa%2Fdoes-not-exist"

    login_page = await client.get(login_url)
    assert 'value="/dashboard/a/does-not-exist"' in login_page.text

    login_resp = await client.post(
        "/dashboard/login",
        data={"email": "dave@example.com", "password": "correct-horse", "next": "/dashboard/a/does-not-exist"},
    )
    assert login_resp.status_code == 303
    # Bounced back to the originally-requested page (which 404s on its own
    # merits — the artifact doesn't exist — but the redirect itself worked).
    assert login_resp.headers["location"] == "/dashboard/a/does-not-exist"


async def test_create_personal_token_from_dashboard(client):
    await make_user(email="dave@example.com", password="correct-horse")
    await _login(client, "dave@example.com", "correct-horse")

    resp = await client.post("/dashboard/connections/tokens", data={"label": "laptop"})
    assert resp.status_code == 200

    # Token is rendered exactly once on the confirmation page.
    match = re.search(r'id="token">([^<]+)</span>', resp.text)
    assert match is not None
    token = match.group(1)

    async with session_scope() as session:
        connection = await auth.resolve_connection(session, token)
    assert connection is not None
    assert connection.label == "laptop"

    connections_resp = await client.get("/dashboard/connections")
    assert "laptop" in connections_resp.text
    assert "Personal token" in connections_resp.text


async def test_revoking_a_personal_token_connection_blocks_it(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id, label="claude-code")

    await _login(client, "dave@example.com", "correct-horse")

    connections_resp = await client.get("/dashboard/connections")
    assert connections_resp.status_code == 200
    assert "claude-code" in connections_resp.text
    assert "Personal token" in connections_resp.text

    revoke_resp = await client.post(f"/dashboard/connections/{connection_id}/revoke")
    assert revoke_resp.status_code == 303

    async with session_scope() as session:
        # Same lookup MCPAuthMiddleware performs for a personal token — the
        # token itself is unaffected, but the connection is now revoked.
        connection = await auth.resolve_connection(session, f"token-for-{connection_id}")
    assert connection is None


async def test_deleting_an_active_connection_is_rejected(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id, label="claude-code")
    await _login(client, "dave@example.com", "correct-horse")

    resp = await client.post(f"/dashboard/connections/{connection_id}/delete")
    assert resp.status_code == 400

    async with session_scope() as session:
        connection = (
            await session.execute(select(Connection).where(Connection.id == connection_id))
        ).scalar_one_or_none()
    assert connection is not None


async def test_deleting_a_revoked_empty_connection_removes_it(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id, label="unused")
    await _login(client, "dave@example.com", "correct-horse")

    await client.post(f"/dashboard/connections/{connection_id}/revoke")

    resp = await client.post(f"/dashboard/connections/{connection_id}/delete")
    assert resp.status_code == 303

    async with session_scope() as session:
        connection = (
            await session.execute(select(Connection).where(Connection.id == connection_id))
        ).scalar_one_or_none()
    assert connection is None

    connections_resp = await client.get("/dashboard/connections")
    assert "unused" not in connections_resp.text


async def test_deleting_a_revoked_connection_with_artifacts_is_blocked(client):
    user_id = await make_user(email="dave@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id, label="claude-code")
    async with session_scope() as session:
        await tools.publish_artifact(session, connection_id, "hello", "markdown")

    await _login(client, "dave@example.com", "correct-horse")
    await client.post(f"/dashboard/connections/{connection_id}/revoke")

    resp = await client.post(f"/dashboard/connections/{connection_id}/delete")
    assert resp.status_code == 400
    assert "1 artifact(s)" in resp.text

    async with session_scope() as session:
        connection = (
            await session.execute(select(Connection).where(Connection.id == connection_id))
        ).scalar_one_or_none()
    assert connection is not None  # still there, just revoked


async def test_logout_clears_session(client):
    await make_user(email="dave@example.com", password="correct-horse")
    await _login(client, "dave@example.com", "correct-horse")
    assert (await client.get("/dashboard")).status_code == 200

    logout_resp = await client.post("/dashboard/logout")
    assert logout_resp.status_code == 303

    assert (await client.get("/dashboard")).status_code == 303
