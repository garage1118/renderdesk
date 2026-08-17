import uuid
from datetime import timedelta

from sqlalchemy import select

from renderdesk.db import session_scope
from renderdesk.models import OAuthAuthorizationCode, OAuthClient, Session, User, utcnow
from renderdesk.oauth_provider import UNUSED_CLIENT_TTL, sweep_expired_oauth_rows
from renderdesk.rate_limit import _attempts, is_rate_limited, record_attempt, sweep_stale_attempts
from renderdesk.session_auth import (
    _LOGIN_LOCKOUT_WINDOW,
    _failed_logins,
    create_session,
    is_login_rate_limited,
    record_failed_login,
    sweep_expired_sessions,
    sweep_stale_failed_logins,
)

from .conftest import make_user


async def _user() -> User:
    user_id = await make_user()
    async with session_scope() as session:
        return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def _session_count() -> int:
    async with session_scope() as session:
        return len((await session.execute(select(Session))).scalars().all())


async def test_sweep_deletes_expired_sessions_but_keeps_live_ones():
    # resolve_session only cleans up a row when someone presents that exact
    # token; a session nobody returns to is otherwise never reconsidered.
    user = await _user()
    async with session_scope() as session:
        await create_session(session, user)
        expired = Session(
            id="expired-1",
            user_id=user.id,
            token_hash="stale-hash",
            expires_at=utcnow() - timedelta(days=1),
        )
        session.add(expired)
        await session.commit()

    assert await _session_count() == 2
    removed = await sweep_expired_sessions()
    assert removed == 1
    assert await _session_count() == 1

    async with session_scope() as session:
        remaining = (await session.execute(select(Session))).scalars().all()
    assert remaining[0].id != "expired-1"


async def test_sweep_is_a_no_op_when_nothing_has_expired():
    user = await _user()
    async with session_scope() as session:
        await create_session(session, user)

    assert await sweep_expired_sessions() == 0
    assert await _session_count() == 1


def test_sweep_drops_stale_rate_limit_entries_but_keeps_active_ones():
    # Keys are client IPs picked by unauthenticated callers, so entries that
    # are never revisited would otherwise accumulate for the process's life.
    record_attempt("login_ip", "10.0.0.1")
    _attempts[("login_ip", "10.0.0.2")] = [utcnow() - timedelta(hours=6)]
    _attempts[("register", "10.0.0.3")] = [utcnow() - timedelta(minutes=30)]

    removed = sweep_stale_attempts()

    assert removed == 1
    assert ("login_ip", "10.0.0.2") not in _attempts
    # Recent attempt: still inside its window.
    assert ("login_ip", "10.0.0.1") in _attempts
    # 30 min old, but /register's window is an hour — must survive, which is
    # why the sweep uses the longest configured window rather than one of them.
    assert ("register", "10.0.0.3") in _attempts


def test_rate_limit_sweep_never_drops_an_entry_still_blocking():
    for _ in range(5):
        record_attempt("register", "10.0.0.9")
    assert is_rate_limited("register", "10.0.0.9", 5, timedelta(hours=1))

    sweep_stale_attempts()

    assert is_rate_limited("register", "10.0.0.9", 5, timedelta(hours=1))


def test_sweep_drops_stale_failed_login_entries_but_keeps_active_ones():
    # The key is the *submitted* email — chosen outright by the caller.
    record_failed_login("recent@example.com")
    _failed_logins["ancient@example.com"] = [utcnow() - _LOGIN_LOCKOUT_WINDOW - timedelta(minutes=1)]

    removed = sweep_stale_failed_logins()

    assert removed == 1
    assert "ancient@example.com" not in _failed_logins
    assert "recent@example.com" in _failed_logins


def test_failed_login_sweep_never_clears_an_active_lockout():
    for _ in range(5):
        record_failed_login("locked@example.com")
    assert is_login_rate_limited("locked@example.com")

    sweep_stale_failed_logins()

    assert is_login_rate_limited("locked@example.com")


async def test_oauth_sweep_does_not_delete_old_client_with_unexpired_pending_code():
    # Regression for CLAUDE-SECURITY-RESULTS.md F14: a client old enough to
    # be swept can still have an *unexpired* pending authorization code —
    # /authorize creates one on every hit, unauthenticated, with its own
    # independent 10-minute expiry — so deleting the client used to violate
    # the foreign key and raise, killing the sweep loop for good.
    client_id = f"old-client-{uuid.uuid4()}"
    async with session_scope() as session:
        session.add(
            OAuthClient(
                client_id=client_id,
                metadata_json={"client_id": client_id, "redirect_uris": ["https://client.example/cb"]},
                created_at=utcnow() - UNUSED_CLIENT_TTL - timedelta(days=1),
            )
        )
        await session.commit()
    async with session_scope() as session:
        session.add(
            OAuthAuthorizationCode(
                id=str(uuid.uuid4()),
                client_id=client_id,
                user_id=None,
                redirect_uri="https://client.example/cb",
                code_challenge="challenge",
                approved=False,
                code_hash=None,
                expires_at=utcnow() + timedelta(minutes=10),
            )
        )
        await session.commit()

    await sweep_expired_oauth_rows()  # must not raise

    async with session_scope() as session:
        client = (
            await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        ).scalar_one_or_none()
    assert client is not None  # still referenced, so not swept yet


async def test_oauth_sweep_deletes_old_unused_client_with_no_references():
    client_id = f"abandoned-client-{uuid.uuid4()}"
    async with session_scope() as session:
        session.add(
            OAuthClient(
                client_id=client_id,
                metadata_json={"client_id": client_id, "redirect_uris": ["https://client.example/cb"]},
                created_at=utcnow() - UNUSED_CLIENT_TTL - timedelta(days=1),
            )
        )
        await session.commit()

    await sweep_expired_oauth_rows()

    async with session_scope() as session:
        client = (
            await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
        ).scalar_one_or_none()
    assert client is None
