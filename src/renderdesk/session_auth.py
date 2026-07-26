import uuid
from datetime import timedelta
from urllib.parse import quote

import bcrypt
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.auth import generate_token, hash_token
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.models import Session, User, utcnow

SESSION_COOKIE_NAME = "renderdesk_session"
LOGIN_PATH = "/dashboard/login"


def verify_password(user: User, password: str) -> bool:
    return bcrypt.checkpw(password.encode(), user.password_hash.encode())


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
    if session_row is None or session_row.expires_at <= utcnow():
        return None

    user_result = await db_session.execute(select(User).where(User.id == session_row.user_id))
    return user_result.scalar_one_or_none()


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
    # redirect off the login page.
    if path.startswith("/") and not path.startswith("//"):
        return path
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
