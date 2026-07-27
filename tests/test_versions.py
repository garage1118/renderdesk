import pytest

from renderdesk import tools, versions
from renderdesk.db import session_scope
from renderdesk.tools import NotFoundError

from .conftest import make_connection, make_user


async def _publish_and_update(user_id: str, connection_id: str, updates: int) -> str:
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown", "Doc")
    artifact_id = published["artifact_id"]
    base_version = 1
    for i in range(updates):
        async with session_scope() as session:
            result = await tools.update_artifact(
                session, connection_id, artifact_id, f"v{i + 2}", base_version=base_version
            )
        base_version = result["version"]
    return artifact_id


async def test_list_versions_returns_all_in_descending_order_with_current_flagged():
    owner_id = await make_user(email="versions-owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=2)

    async with session_scope() as session:
        history = await versions.list_versions(session, owner_id, artifact_id)

    assert [v["version"] for v in history] == [3, 2, 1]
    assert history[0]["is_current"] is True
    assert history[1]["is_current"] is False
    assert history[2]["is_current"] is False
    assert history[0]["byte_size"] == len("v3".encode())


async def test_list_versions_by_non_owner_raises():
    owner_id = await make_user(email="versions-owner2@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=1)
    other_user_id = await make_user(email="versions-not-owner@example.com")

    async with session_scope() as session:
        with pytest.raises(NotFoundError):
            await versions.list_versions(session, other_user_id, artifact_id)


async def test_get_version_returns_the_requested_snapshot():
    owner_id = await make_user(email="versions-owner3@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=2)

    async with session_scope() as session:
        v1 = await versions.get_version(session, owner_id, artifact_id, 1)
    assert v1.content == "v1"

    async with session_scope() as session:
        with pytest.raises(NotFoundError):
            await versions.get_version(session, owner_id, artifact_id, 99)


async def test_delete_version_removes_a_superseded_version():
    owner_id = await make_user(email="versions-owner4@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=2)

    async with session_scope() as session:
        await versions.delete_version(session, owner_id, artifact_id, 1)

    async with session_scope() as session:
        history = await versions.list_versions(session, owner_id, artifact_id)
    assert [v["version"] for v in history] == [3, 2]


async def test_delete_current_version_is_rejected():
    owner_id = await make_user(email="versions-owner5@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=1)

    async with session_scope() as session:
        with pytest.raises(versions.CannotDeleteCurrentVersionError):
            await versions.delete_version(session, owner_id, artifact_id, 2)

    async with session_scope() as session:
        history = await versions.list_versions(session, owner_id, artifact_id)
    assert len(history) == 2  # nothing was deleted


async def test_prune_old_versions_keeps_only_current():
    owner_id = await make_user(email="versions-owner6@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish_and_update(owner_id, connection_id, updates=3)

    async with session_scope() as session:
        deleted_count = await versions.prune_old_versions(session, owner_id, artifact_id)
    assert deleted_count == 3

    async with session_scope() as session:
        history = await versions.list_versions(session, owner_id, artifact_id)
    assert [v["version"] for v in history] == [4]
    assert history[0]["is_current"] is True
