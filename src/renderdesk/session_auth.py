import uuid
from datetime import datetime, timedelta
from urllib.parse import quote, urlparse, urlunparse

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.auth import generate_token, hash_token
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.models import Session, User, utcnow

SESSION_COOKIE_NAME = "renderdesk_session"
LOGIN_PATH = "/dashboard/login"

# In-memory fixed-window lockout, keyed by the attempted email — good enough
# for a single-process deploy (see Dockerfile: one uvicorn worker, no
# multi-process fan-out) without pulling in a rate-limiting dependency or
# persisting attempt history across restarts.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_LOCKOUT_WINDOW = timedelta(minutes=15)
_failed_logins: dict[str, list[datetime]] = {}


def is_login_rate_limited(email: str) -> bool:
    cutoff = utcnow() - _LOGIN_LOCKOUT_WINDOW
    attempts = [t for t in _failed_logins.get(email, []) if t > cutoff]
    if attempts:
        _failed_logins[email] = attempts
    else:
        _failed_logins.pop(email, None)
    return len(attempts) >= _LOGIN_MAX_ATTEMPTS


def record_failed_login(email: str) -> None:
    _failed_logins.setdefault(email, []).append(utcnow())


def clear_failed_logins(email: str) -> None:
    _failed_logins.pop(email, None)


def sweep_stale_failed_logins() -> int:
    """Drops entries whose newest failure already fell out of the lockout
    window. Same unbounded-growth shape as rate_limit.sweep_stale_attempts,
    and worse in one respect: the key here is the *submitted* email, so an
    unauthenticated caller picks it outright and a fresh address per attempt
    adds an entry that nothing revisits. Called from app.py's sweep loop."""
    cutoff = utcnow() - _LOGIN_LOCKOUT_WINDOW
    stale = [email for email, attempts in _failed_logins.items() if not attempts or max(attempts) <= cutoff]
    for email in stale:
        del _failed_logins[email]
    return len(stale)


# A fixed dummy hash (same cost factor as a real one, bcrypt's default 12
# rounds) verified against whenever there's no real hash to check —
# account absent, or an OIDC-provisioned user with no password set. Without
# this, that path returns False after a single indexed lookup while a real
# account takes the hundreds of milliseconds bcrypt.checkpw costs by
# design, which is a two-to-three-order-of-magnitude timing side channel
# an unauthenticated caller can use to enumerate which emails have
# accounts (CLAUDE-SECURITY-RESULTS.md F12). Generated once at import so
# every "no real hash" call costs the same as a real check, every time.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"renderdesk-timing-placeholder", bcrypt.gensalt(rounds=12)).decode()


def verify_password(user: User | None, password: str) -> bool:
    password_hash = user.password_hash if user is not None else None
    if password_hash is None:
        # Always spend a real bcrypt verification even though the result
        # is discarded — see _DUMMY_PASSWORD_HASH above.
        try:
            bcrypt.checkpw(password.encode(), _DUMMY_PASSWORD_HASH.encode())
        except ValueError:
            pass
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        # bcrypt 5.x raises rather than truncating for passwords >72 bytes.
        # Without this, that case 500s instead of failing like any other
        # wrong password — which, since it's only reachable once a user is
        # known to exist, is an unauthenticated account-enumeration oracle
        # that also skips record_failed_login (the exception fires before
        # it's called), bypassing the lockout entirely.
        return False


async def create_session(db_session: AsyncSession, user: User) -> str:
    token = generate_token()
    db_session.add(
        Session(
            id=str(uuid.uuid4()),
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + timedelta(days=settings.session_expiry_days),
        )
    )
    await db_session.commit()
    return token


async def resolve_session(db_session: AsyncSession, token: str) -> User | None:
    token_hash = hash_token(token)
    result = await db_session.execute(select(Session).where(Session.token_hash == token_hash))
    session_row = result.scalar_one_or_none()
    if session_row is None:
        return None
    if session_row.expires_at <= utcnow():
        # Opportunistic cleanup — since we're already here looking it up,
        # delete it now rather than letting expired rows accumulate forever.
        await db_session.delete(session_row)
        await db_session.commit()
        return None

    user_result = await db_session.execute(select(User).where(User.id == session_row.user_id))
    return user_result.scalar_one_or_none()


async def sweep_expired_sessions() -> int:
    """Deletes every expired session row. resolve_session already cleans one
    up opportunistically, but only when someone presents that exact token —
    a session belonging to anyone who closed the tab and never came back is
    otherwise never looked at again, so the row lives forever. Called on the
    same periodic loop as the OAuth sweep (see app.py)."""
    async with session_scope() as db_session:
        result = await db_session.execute(delete(Session).where(Session.expires_at <= utcnow()))
        await db_session.commit()
    return result.rowcount


async def delete_session(db_session: AsyncSession, token: str) -> None:
    token_hash = hash_token(token)
    result = await db_session.execute(select(Session).where(Session.token_hash == token_hash))
    session_row = result.scalar_one_or_none()
    if session_row is not None:
        await db_session.delete(session_row)
        await db_session.commit()


def safe_next_path(path: str) -> str:
    # Only ever redirect back to a same-site relative path — a `next` value
    # like "//evil.com" or "https://evil.com" would otherwise be an open
    # redirect off the login page. Backslashes are rejected too: browsers
    # normalize "/\evil.com" to the protocol-relative "//evil.com" per the
    # WHATWG URL spec, so a bare startswith("//") check alone misses it.
    # all(c.isprintable()...) is defense in depth against embedded CRLF/
    # control characters reaching a Location header, on top of whatever
    # Starlette already rejects.
    #
    # A run of *any* number of leading slashes is rejected, not just
    # exactly two: urlparse('///evil.com') reports scheme='' and
    # netloc='' (it only recognizes an authority after precisely "//"),
    # so scheme/netloc alone let "///evil.com" and "////evil.com" through
    # unchanged — and the WHATWG URL spec's special-authority-ignore-
    # slashes state means a browser resolves any of those against a
    # Location header the same way it resolves "//evil.com".
    parsed = urlparse(path)
    if (
        not parsed.scheme
        and not parsed.netloc
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and all(c.isprintable() for c in path)
    ):
        # Re-serialize from the parsed path/query rather than returning the
        # attacker-influenced raw string verbatim — belt-and-braces against
        # any other parsing quirk the checks above didn't anticipate.
        return urlunparse(("", "", parsed.path, "", parsed.query, ""))
    return "/dashboard"


async def require_current_user(request: Request) -> User:
    """FastAPI dependency for every dashboard route except login itself.
    Deliberately a separate code path from MCPAuthMiddleware (auth.py) — a
    dashboard session cookie must never be usable where an MCP token is
    expected, or vice versa."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        async with session_scope() as db_session:
            user = await resolve_session(db_session, token)
        if user is not None:
            return user
    next_path = safe_next_path(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    login_url = f"{LOGIN_PATH}?next={quote(next_path, safe='')}"
    raise HTTPException(status_code=303, headers={"Location": login_url})
