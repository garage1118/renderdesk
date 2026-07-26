import pytest

from renderdesk import shares, tools
from renderdesk.db import session_scope

from .conftest import make_connection, make_user


async def _publish(connection_id: str) -> str:
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    return published["artifact_id"]


async def test_share_then_visible_to_recipient():
    owner_id = await make_user(email="owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)
    recipient_id = await make_user(email="recipient@example.com")

    async with session_scope() as session:
        result = await shares.share_artifact(session, connection_id, artifact_id, "recipient@example.com")
    assert result["already_shared"] is False

    async with session_scope() as session:
        shared = await shares.list_shared_with_user(session, recipient_id)
    assert len(shared) == 1
    assert shared[0]["artifact_id"] == artifact_id


async def test_share_with_unknown_email_raises():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)

    async with session_scope() as session:
        with pytest.raises(shares.RecipientNotFoundError):
            await shares.share_artifact(session, connection_id, artifact_id, "nobody@example.com")


async def test_share_artifact_you_dont_own_raises():
    connection_a = await make_connection("a")
    connection_b = await make_connection("b")
    artifact_id = await _publish(connection_a)
    await make_user(email="recipient2@example.com")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await shares.share_artifact(session, connection_b, artifact_id, "recipient2@example.com")


async def test_share_with_self_is_rejected():
    owner_id = await make_user(email="solo@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)

    async with session_scope() as session:
        with pytest.raises(shares.SelfShareError):
            await shares.share_artifact(session, connection_id, artifact_id, "solo@example.com")


async def test_resharing_is_idempotent():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    await make_user(email="recipient3@example.com")

    async with session_scope() as session:
        first = await shares.share_artifact(session, connection_id, artifact_id, "recipient3@example.com")
    assert first["already_shared"] is False

    async with session_scope() as session:
        second = await shares.share_artifact(session, connection_id, artifact_id, "recipient3@example.com")
    assert second["already_shared"] is True
