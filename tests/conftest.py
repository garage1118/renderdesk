import os
import tempfile

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("RENDERDESK_DATABASE_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("RENDERDESK_PUBLIC_BASE_URL", "http://localhost:8000")

import uuid
from datetime import timedelta

import bcrypt
import pytest

from renderdesk.auth import hash_token
from renderdesk.config import settings
from renderdesk.db import Base, engine, session_scope
from renderdesk.models import Connection, User, utcnow


@pytest.fixture(autouse=True)
async def _reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
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
