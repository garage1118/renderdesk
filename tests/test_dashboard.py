import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from renderdesk import auth, comments, shares, tools, versions
from renderdesk.app import app
from renderdesk.db import session_scope
from renderdesk.models import Connection, Session, utcnow
from renderdesk.session_auth import resolve_session, safe_next_path

from .conftest import make_connection, make_user


def test_safe_next_path_rejects_crlf_and_control_chars():
    assert safe_next_path("/dashboard/a/x\r\nSet-Cookie: evil=1") == "/dashboard"
    assert safe_next_path("/dashboard\x00/a") == "/dashboard"
    assert safe_next_path("//evil.com") == "/dashboard"
    assert safe_next_path("/dashboard/a/x") == "/dashboard/a/x"


async def test_resolve_session_deletes_expired_row():
    user_id = await make_user(email="expired-session@example.com")
    async with session_scope() as session:
        session.add(
            Session(
                id="expired-session-1",
                user_id=user_id,
                token_hash=auth.hash_token("some-expired-token"),
                expires_at=utcnow() - timedelta(days=1),
            )
        )
        await session.commit()

    async with session_scope() as session:
        user = await resolve_session(session, "some-expired-token")
    assert user is None

    async with session_scope() as session:
        remaining = (
            await session.execute(select(Session).where(Session.id == "expired-session-1"))
        ).scalar_one_or_none()
    assert remaining is None


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


async def _csrf_token(client: httpx.AsyncClient) -> str:
    token = client.cookies.get("csrf_token")
    if token is None:
        await client.get("/dashboard/login")
        token = client.cookies["csrf_token"]
    return token


async def _post(client: httpx.AsyncClient, url: str, data: dict | None = None) -> httpx.Response:
    payload = dict(data or {})
    payload.setdefault("csrf_token", await _csrf_token(client))
    return await client.post(url, data=payload)


async def _login(client, email, password):
    return await _post(client, "/dashboard/login", {"email": email, "password": password})


async def test_unauthenticated_dashboard_redirects_to_login(client):
    resp = await client.get("/dashboard")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard/login?next=%2Fdashboard"


async def test_post_missing_csrf_token_is_rejected(client):
    await make_user(email="csrf-missing@example.com", password="correct-horse")
    await client.get("/dashboard/login")  # seeds the csrf_token cookie, no form field sent back

    resp = await client.post(
        "/dashboard/login", data={"email": "csrf-missing@example.com", "password": "correct-horse"}
    )
    assert resp.status_code == 422
    assert "renderdesk_session" not in client.cookies


async def test_post_mismatched_csrf_token_is_rejected(client):
    await make_user(email="csrf-mismatch@example.com", password="correct-horse")
    await client.get("/dashboard/login")  # seeds the real cookie value

    resp = await client.post(
        "/dashboard/login",
        data={
            "email": "csrf-mismatch@example.com",
            "password": "correct-horse",
            "csrf_token": "attacker-supplied-value",
        },
    )
    assert resp.status_code == 403
    assert "renderdesk_session" not in client.cookies


async def test_post_with_correct_csrf_token_succeeds(client):
    await make_user(email="csrf-ok@example.com", password="correct-horse")
    resp = await _login(client, "csrf-ok@example.com", "correct-horse")
    assert resp.status_code == 303
    assert "renderdesk_session" in client.cookies


async def test_rendered_forms_embed_the_real_csrf_token(client):
    await make_user(email="csrf-embed@example.com", password="correct-horse")
    login_page = await client.get("/dashboard/login")
    embedded = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert embedded is not None
    assert embedded.group(1) == client.cookies["csrf_token"]


async def test_wrong_password_rejected(client):
    await make_user(email="dave@example.com", password="correct-horse")

    resp = await _login(client, "dave@example.com", "wrong-password")

    assert resp.status_code == 401
    assert "renderdesk_session" not in client.cookies


async def test_repeated_failed_logins_are_rate_limited(client):
    await make_user(email="bruteforce@example.com", password="correct-horse")

    for _ in range(5):
        resp = await _login(client, "bruteforce@example.com", "wrong-password")
        assert resp.status_code == 401

    # 6th attempt is locked out even with a wrong password...
    resp = await _login(client, "bruteforce@example.com", "wrong-password")
    assert resp.status_code == 429

    # ...and even with the correct one.
    resp = await _login(client, "bruteforce@example.com", "correct-horse")
    assert resp.status_code == 429
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
    await make_user(email="dave@example.com", password="correct-horse")
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

    comment_resp = await _post(client, f"/dashboard/a/{artifact_id}/comments", data={"body": "please fix the title"})
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

    comment_resp = await _post(client, f"/dashboard/a/{artifact_id}/comments", data={"body": "looks great"})
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

    login_resp = await _post(client, 
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

    resp = await _post(client, "/dashboard/connections/tokens", data={"label": "laptop"})
    assert resp.status_code == 200

    # Token is rendered exactly once on the confirmation page.
    match = re.search(r'id="token"[^>]*>([^<]+)</span>', resp.text)
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

    revoke_resp = await _post(client, f"/dashboard/connections/{connection_id}/revoke")
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

    resp = await _post(client, f"/dashboard/connections/{connection_id}/delete")
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

    await _post(client, f"/dashboard/connections/{connection_id}/revoke")

    resp = await _post(client, f"/dashboard/connections/{connection_id}/delete")
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
    await _post(client, f"/dashboard/connections/{connection_id}/revoke")

    resp = await _post(client, f"/dashboard/connections/{connection_id}/delete")
    assert resp.status_code == 400
    assert "1 artifact(s)" in resp.text

    async with session_scope() as session:
        connection = (
            await session.execute(select(Connection).where(Connection.id == connection_id))
        ).scalar_one_or_none()
    assert connection is not None  # still there, just revoked


async def test_owner_can_share_and_unshare_artifact_from_dashboard(client):
    owner_id = await make_user(email="owner2@example.com", password="owner-pass")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    artifact_id = published["artifact_id"]
    await make_user(email="recipient5@example.com")

    await _login(client, "owner2@example.com", "owner-pass")

    share_resp = await _post(client, f"/dashboard/a/{artifact_id}/share", data={"email": "recipient5@example.com"})
    assert share_resp.status_code == 303

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}")
    assert "recipient5@example.com" in detail_resp.text

    async with session_scope() as session:
        share_list = await shares.list_shares(session, owner_id, artifact_id)
    assert len(share_list) == 1

    unshare_resp = await _post(client, 
        f"/dashboard/a/{artifact_id}/shares/{share_list[0]['share_id']}/unshare"
    )
    assert unshare_resp.status_code == 303

    async with session_scope() as session:
        assert await shares.list_shares(session, owner_id, artifact_id) == []


async def test_share_with_unknown_email_shows_error_on_page(client):
    owner_id = await make_user(email="owner3@example.com", password="owner-pass")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")

    await _login(client, "owner3@example.com", "owner-pass")

    resp = await _post(client, 
        f"/dashboard/a/{published['artifact_id']}/share", data={"email": "nobody-here@example.com"}
    )
    assert resp.status_code == 400
    assert "no user with email" in resp.text


async def test_recipient_cannot_share_or_delete_artifact_shared_with_them(client):
    owner_id = await make_user(email="owner4@example.com", password="owner-pass")
    owner_connection = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, owner_connection, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await make_user(email="recipient6@example.com", password="recipient-pass")
    async with session_scope() as session:
        await shares.share_artifact(session, owner_connection, artifact_id, "recipient6@example.com")

    await _login(client, "recipient6@example.com", "recipient-pass")

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}")
    assert detail_resp.status_code == 200
    assert "Delete artifact" not in detail_resp.text

    share_resp = await _post(client, f"/dashboard/a/{artifact_id}/share", data={"email": "someone@example.com"})
    assert share_resp.status_code == 404

    delete_resp = await _post(client, f"/dashboard/a/{artifact_id}/delete")
    assert delete_resp.status_code == 404

    async with session_scope() as session:
        # Still there — the recipient's attempt to delete it was rejected.
        fetched = await tools.get_artifact(session, owner_connection, artifact_id)
    assert fetched["artifact_id"] == artifact_id


async def test_owner_reassigns_artifact_to_another_connection(client):
    owner_id = await make_user(email="owner6@example.com", password="owner-pass")
    connection_a = await make_connection("laptop", user_id=owner_id)
    connection_b = await make_connection("phone", user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await _login(client, "owner6@example.com", "owner-pass")

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}")
    assert "Move to another connection" in detail_resp.text
    assert "phone" in detail_resp.text

    reassign_resp = await _post(client, 
        f"/dashboard/a/{artifact_id}/reassign", data={"target_connection_id": connection_b}
    )
    assert reassign_resp.status_code == 303

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, connection_a, artifact_id)
        fetched = await tools.get_artifact(session, connection_b, artifact_id)
    assert fetched["artifact_id"] == artifact_id

    list_resp = await client.get("/dashboard")
    assert "phone" in list_resp.text


async def test_reassign_panel_hidden_with_only_one_connection(client):
    owner_id = await make_user(email="owner7@example.com", password="owner-pass")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")

    await _login(client, "owner7@example.com", "owner-pass")

    detail_resp = await client.get(f"/dashboard/a/{published['artifact_id']}")
    assert "Move to another connection" not in detail_resp.text


async def test_reassign_by_non_owner_via_dashboard_is_rejected(client):
    owner_id = await make_user(email="owner8@example.com", password="owner-pass")
    owner_connection = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, owner_connection, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await make_user(email="recipient7@example.com", password="recipient-pass")
    async with session_scope() as session:
        await shares.share_artifact(session, owner_connection, artifact_id, "recipient7@example.com")

    await _login(client, "recipient7@example.com", "recipient-pass")
    resp = await _post(client, 
        f"/dashboard/a/{artifact_id}/reassign", data={"target_connection_id": "does-not-matter"}
    )
    assert resp.status_code == 404


async def test_owner_deletes_artifact_from_dashboard(client):
    owner_id = await make_user(email="owner5@example.com", password="owner-pass")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await _login(client, "owner5@example.com", "owner-pass")

    delete_resp = await _post(client, f"/dashboard/a/{artifact_id}/delete")
    assert delete_resp.status_code == 303
    assert delete_resp.headers["location"] == "/dashboard"

    dashboard_resp = await client.get("/dashboard")
    assert artifact_id not in dashboard_resp.text

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, connection_id, artifact_id)


async def test_logout_clears_session(client):
    await make_user(email="dave@example.com", password="correct-horse")
    await _login(client, "dave@example.com", "correct-horse")
    assert (await client.get("/dashboard")).status_code == 200

    logout_resp = await _post(client, "/dashboard/logout")
    assert logout_resp.status_code == 303

    assert (await client.get("/dashboard")).status_code == 303


async def test_version_history_page_lists_versions_and_allows_pruning(client):
    user_id = await make_user(email="versions-dash@example.com", password="correct-horse")
    connection_id = await make_connection(user_id=user_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown")
    artifact_id = published["artifact_id"]
    async with session_scope() as session:
        await tools.update_artifact(session, connection_id, artifact_id, "v2", base_version=1)

    await _login(client, "versions-dash@example.com", "correct-horse")

    history_resp = await client.get(f"/dashboard/a/{artifact_id}/versions")
    assert history_resp.status_code == 200
    assert "Current" in history_resp.text
    assert "Prune old versions" in history_resp.text

    detail_resp = await client.get(f"/dashboard/a/{artifact_id}/versions/1")
    assert detail_resp.status_code == 200
    assert "v1" in detail_resp.text

    # Deleting the current version is rejected...
    delete_current_resp = await _post(client, f"/dashboard/a/{artifact_id}/versions/2/delete")
    assert delete_current_resp.status_code == 400

    # ...but the superseded one can be deleted.
    delete_old_resp = await _post(client, f"/dashboard/a/{artifact_id}/versions/1/delete")
    assert delete_old_resp.status_code == 303

    async with session_scope() as session:
        history = await versions.list_versions(session, user_id, artifact_id)
    assert [v["version"] for v in history] == [2]

    # Prune is a no-op with nothing left to remove, but should still succeed.
    prune_resp = await _post(client, f"/dashboard/a/{artifact_id}/versions/prune")
    assert prune_resp.status_code == 303


async def test_version_history_requires_ownership(client):
    owner_id = await make_user(email="versions-owner-dash@example.com", password="owner-pass")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    artifact_id = published["artifact_id"]

    await make_user(email="versions-stranger@example.com", password="stranger-pass")
    await _login(client, "versions-stranger@example.com", "stranger-pass")

    resp = await client.get(f"/dashboard/a/{artifact_id}/versions")
    assert resp.status_code == 404
