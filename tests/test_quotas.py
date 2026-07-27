import asyncio

import pytest

from renderdesk import tools
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.quotas import QuotaExceededError, quota_lock

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


async def test_quota_lock_serializes_same_connection_but_not_different_ones():
    order = []

    async def hold(name, lock):
        async with lock:
            order.append(f"{name}-start")
            await asyncio.sleep(0.05)
            order.append(f"{name}-end")

    # Same connection_id -> the same Lock instance -> must not interleave.
    await asyncio.gather(hold("a1", quota_lock("conn-a")), hold("a2", quota_lock("conn-a")))
    assert order in (
        ["a1-start", "a1-end", "a2-start", "a2-end"],
        ["a2-start", "a2-end", "a1-start", "a1-end"],
    )

    # Different connection_ids -> independent locks -> free to overlap.
    order.clear()
    await asyncio.gather(hold("b", quota_lock("conn-b")), hold("c", quota_lock("conn-c")))
    assert order[0].endswith("-start") and order[1].endswith("-start")


async def test_concurrent_publishes_cannot_both_bypass_the_artifact_count_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_artifacts_per_connection", 1)
    connection_id = await make_connection()

    async def _publish(body: str):
        async with session_scope() as session:
            return await tools.publish_artifact(session, connection_id, body, "markdown")

    results = await asyncio.gather(_publish("one"), _publish("two"), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, QuotaExceededError)]
    assert len(successes) == 1
    assert len(failures) == 1


async def test_concurrent_updates_cannot_both_win_the_version_conflict_check():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown")

    async def _update(body: str):
        async with session_scope() as session:
            return await tools.update_artifact(
                session, connection_id, published["artifact_id"], body, base_version=1
            )

    results = await asyncio.gather(_update("v2-a"), _update("v2-b"), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, tools.VersionConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
