import os
import tempfile

_tmpdir = tempfile.mkdtemp()
os.environ.setdefault("RENDERDESK_DATABASE_PATH", os.path.join(_tmpdir, "test.db"))
os.environ.setdefault("RENDERDESK_PUBLIC_BASE_URL", "http://testserver")

import uuid
from datetime import timedelta

import pytest

from renderdesk.auth import hash_token
from renderdesk.config import settings
from renderdesk.db import Base, engine, session_scope
from renderdesk.models import Connection, utcnow


@pytest.fixture(autouse=True)
async def _reset_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


async def make_connection(label: str = "test") -> str:
    connection_id = str(uuid.uuid4())
    async with session_scope() as session:
        session.add(
            Connection(
                id=connection_id,
                token_hash=hash_token(f"token-for-{connection_id}"),
                label=label,
                expires_at=utcnow() + timedelta(days=settings.token_expiry_days),
            )
        )
        await session.commit()
    return connection_id
