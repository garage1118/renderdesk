import uuid

import pytest
from sqlalchemy import select

from renderdesk import shares, tools
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.models import ArtifactShare, ArtifactVersion, Comment, Connection
from renderdesk.quotas import QuotaExceededError

from .conftest import make_connection, make_user


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


async def test_delete_artifact_removes_it_and_its_versions_comments_and_shares():
    owner_id = await make_user(email="deletes-stuff@example.com")
    connection_id = await make_connection(user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "v1", "markdown")
        artifact_id = published["artifact_id"]
        await tools.update_artifact(session, connection_id, artifact_id, "v2", base_version=1)
        session.add(
            Comment(id=str(uuid.uuid4()), artifact_id=artifact_id, author_user_id=owner_id, body="hi")
        )
        await session.commit()

    await make_user(email="recipient-of-doomed-artifact@example.com")

    async with session_scope() as session:
        await shares.share_artifact(
            session, connection_id, artifact_id, "recipient-of-doomed-artifact@example.com"
        )

    async with session_scope() as session:
        await tools.delete_artifact(session, owner_id, artifact_id)

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, connection_id, artifact_id)
        assert (
            await session.execute(select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id))
        ).scalars().all() == []
        assert (
            await session.execute(select(Comment).where(Comment.artifact_id == artifact_id))
        ).scalars().all() == []
        assert (
            await session.execute(select(ArtifactShare).where(ArtifactShare.artifact_id == artifact_id))
        ).scalars().all() == []


async def test_publish_code_artifact_with_language_round_trips():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(
            session, connection_id, "def foo():\n    pass\n", "code", "foo.py", language="python"
        )

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_id, published["artifact_id"], include_content=True)
    assert fetched["format"] == "code"
    assert fetched["language"] == "python"
    assert fetched["content"] == "def foo():\n    pass\n"

    async with session_scope() as session:
        listing = await tools.list_artifacts(session, connection_id)
    assert listing[0]["language"] == "python"


async def test_update_code_artifact_can_change_language():
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "print(1)", "code", language="python")

    async with session_scope() as session:
        await tools.update_artifact(
            session,
            connection_id,
            published["artifact_id"],
            "console.log(1)",
            base_version=1,
            language="javascript",
        )

    async with session_scope() as session:
        fetched = await tools.get_artifact(session, connection_id, published["artifact_id"])
    assert fetched["language"] == "javascript"


async def test_delete_artifact_by_non_owner_raises():
    owner_id = await make_user(email="rightful-owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    other_owner_id = await make_user(email="wants-to-delete-it@example.com")

    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "mine", "markdown")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.delete_artifact(session, other_owner_id, published["artifact_id"])


async def test_reassign_moves_artifact_to_another_owned_connection():
    owner_id = await make_user(email="reassigns-stuff@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    connection_b = await make_connection("b", user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")
    artifact_id = published["artifact_id"]

    async with session_scope() as session:
        result = await tools.reassign_artifact_connection(session, owner_id, artifact_id, connection_b)
    assert result == {"artifact_id": artifact_id, "connection_id": connection_b}

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.get_artifact(session, connection_a, artifact_id)
        fetched = await tools.get_artifact(session, connection_b, artifact_id)
    assert fetched["artifact_id"] == artifact_id


async def test_reassign_by_non_owner_raises():
    owner_id = await make_user(email="owns-artifact@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    other_user_id = await make_user(email="not-the-owner2@example.com")
    other_connection = await make_connection("other", user_id=other_user_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await tools.reassign_artifact_connection(
                session, other_user_id, published["artifact_id"], other_connection
            )


async def test_reassign_to_connection_owned_by_someone_else_raises():
    owner_id = await make_user(email="owns-it2@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    someone_else_id = await make_user(email="someone-else@example.com")
    someone_elses_connection = await make_connection("theirs", user_id=someone_else_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")

    async with session_scope() as session:
        with pytest.raises(tools.InvalidTargetConnectionError):
            await tools.reassign_artifact_connection(
                session, owner_id, published["artifact_id"], someone_elses_connection
            )


async def test_reassign_to_revoked_connection_raises():
    owner_id = await make_user(email="owns-it3@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    connection_b = await make_connection("b", user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")

    async with session_scope() as session:
        target = (
            await session.execute(select(Connection).where(Connection.id == connection_b))
        ).scalar_one()
        target.revoked_at = target.created_at
        await session.commit()

    async with session_scope() as session:
        with pytest.raises(tools.InvalidTargetConnectionError):
            await tools.reassign_artifact_connection(
                session, owner_id, published["artifact_id"], connection_b
            )


async def test_reassign_to_same_connection_raises():
    owner_id = await make_user(email="owns-it4@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")

    async with session_scope() as session:
        with pytest.raises(tools.InvalidTargetConnectionError):
            await tools.reassign_artifact_connection(
                session, owner_id, published["artifact_id"], connection_a
            )


async def test_reassign_respects_target_connection_quota(monkeypatch):
    monkeypatch.setattr(settings, "max_artifacts_per_connection", 1)
    owner_id = await make_user(email="owns-it5@example.com")
    connection_a = await make_connection("a", user_id=owner_id)
    connection_b = await make_connection("b", user_id=owner_id)
    async with session_scope() as session:
        await tools.publish_artifact(session, connection_b, "already here", "markdown")
        published = await tools.publish_artifact(session, connection_a, "mine", "markdown")

    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await tools.reassign_artifact_connection(
                session, owner_id, published["artifact_id"], connection_b
            )
