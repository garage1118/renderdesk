import atexit
import os
import shutil
import tempfile

# Has to exist before Settings is constructed (RENDERDESK_DATABASE_PATH is
# read at import time), so this can't wait for a fixture — cleaned up via
# atexit instead of a fixture teardown for the same reason.
_tmpdir = tempfile.mkdtemp()
atexit.register(shutil.rmtree, _tmpdir, ignore_errors=True)
os.environ.setdefault("RENDERDESK_DATABASE_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("RENDERDESK_PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("RENDERDESK_AUTH_SCHEME", "password")

import uuid
from datetime import timedelta

import bcrypt
import pytest

from renderdesk.auth import hash_token
from renderdesk.auth_scheme import ensure_auth_scheme
from renderdesk.config import settings
from renderdesk.db import Base, engine, session_scope
from renderdesk.models import Connection, User, utcnow
from renderdesk.rate_limit import _attempts as _rate_limit_attempts
from renderdesk.session_auth import _failed_logins


@pytest.fixture(autouse=True)
async def _reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture(autouse=True)
async def _reset_auth_scheme(_reset_db):
    # _reset_db wipes app_settings along with everything else each test, so
    # this re-resolves it fresh every time — most tests exercise the
    # password-based dashboard, so "password" is the sane default; a test
    # that needs a different active scheme can override via monkeypatch.
    async with session_scope() as session:
        await ensure_auth_scheme(session, "password")
    yield


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    # Both trackers are in-process module state (session_auth.py,
    # rate_limit.py), not DB-backed, so _reset_db doesn't touch them —
    # clear explicitly between tests. Matters for _attempts especially:
    # httpx.ASGITransport gives every test request the same fixed
    # client.host ("127.0.0.1"), so RateLimitMiddleware's per-IP login
    # bucket would otherwise accumulate across the whole suite and start
    # 429-ing real logins partway through an unrelated test file.
    _failed_logins.clear()
    _rate_limit_attempts.clear()
    yield


async def make_user(email: str | None = None, password: str = "testpassword") -> str:
    user_id = str(uuid.uuid4())
    async with session_scope() as session:
        session.add(
            User(
                id=user_id,
                email=email or f"{user_id}@example.com",
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                created_at=utcnow(),
            )
        )
        await session.commit()
    return user_id


async def make_connection(label: str = "test", user_id: str | None = None) -> str:
    if user_id is None:
        user_id = await make_user()
    connection_id = str(uuid.uuid4())
    async with session_scope() as session:
        session.add(
            Connection(
                id=connection_id,
                user_id=user_id,
                token_hash=hash_token(f"token-for-{connection_id}"),
                label=label,
                expires_at=utcnow() + timedelta(days=settings.token_expiry_days),
            )
        )
        await session.commit()
    return connection_id
