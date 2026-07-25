import pytest

from renderdesk import tools
from renderdesk.db import session_scope

from .conftest import make_connection


async def test_publish_then_get_round_trip():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "<h1>hi</h1>", "html", "My title")

    assert published["version"] == 1
    assert published["url"].endswith(f"/a/{published['artifact_id']}")

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_id, published["artifact_id"], include_content=True)

    assert fetched["content"] == "<h1>hi</h1>"
    assert fetched["title"] == "My title"
    assert fetched["version"] == 1


async def test_update_with_correct_base_version_bumps_version():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown")

    async with session_scope() as session:
        updated = await tools.update_artifact(
            session, connection_id, published["artifact_id"], "v2", base_version=1
        )

    assert updated["version"] == 2

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_id, published["artifact_id"], include_content=True)
    assert fetched["content"] == "v2"
    assert fetched["version"] == 2


async def test_update_with_stale_base_version_raises_and_does_not_mutate():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown")

    async with session_scope() as session:
        with pytest.raises(tools.VersionConflictError):
            await tools.update_artifact(
                session, connection_id, published["artifact_id"], "v2", base_version=99
            )

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_id, published["artifact_id"], include_content=True)
    assert fetched["content"] == "v1"
    assert fetched["version"] == 1


async def test_list_and_get_are_scoped_per_connection():
    connection_a = await make_connection("a")
    connection_b = await make_connection("b")

    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "secret", "markdown")

    async with session_scope() as session:
        listing = await tools.list_artifacts(session, connection_b)
    assert listing == []

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, connection_b, published["artifact_id"])

    async with session_scope() as session:
        listing_a = await tools.list_artifacts(session, connection_a)
    assert len(listing_a) == 1
    assert listing_a[0]["artifact_id"] == published["artifact_id"]
