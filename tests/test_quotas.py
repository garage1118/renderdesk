import pytest

from renderdesk import tools
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.quotas import QuotaExceededError

from .conftest import make_connection


async def test_publish_over_per_artifact_byte_cap_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_bytes_per_artifact", 4)
    connection_id = await make_connection()

    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await tools.publish_artifact(session, connection_id, "way too long", "markdown")


async def test_publish_over_artifact_count_cap_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_artifacts_per_connection", 1)
    connection_id = await make_connection()

    async with session_scope() as session:
        await tools.publish_artifact(session, connection_id, "one", "markdown")

    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await tools.publish_artifact(session, connection_id, "two", "markdown")


async def test_publish_over_total_bytes_cap_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_total_bytes_per_connection", 5)
    connection_id = await make_connection()

    async with session_scope() as session:
        await tools.publish_artifact(session, connection_id, "abc", "markdown")

    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await tools.publish_artifact(session, connection_id, "abc", "markdown")


async def test_update_over_per_artifact_byte_cap_rejected(monkeypatch):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "short", "markdown")

    monkeypatch.setattr(settings, "max_bytes_per_artifact", 4)
    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await tools.update_artifact(
                session, connection_id, published["artifact_id"], "way too long now", base_version=1
            )
