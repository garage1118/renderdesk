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


async def test_share_from_dashboard_then_list_and_unshare():
    owner_id = await make_user(email="dashboard-owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)
    await make_user(email="dashboard-recipient@example.com")

    async with session_scope() as session:
        result = await shares.share_artifact_from_dashboard(
            session, owner_id, artifact_id, "dashboard-recipient@example.com"
        )
    assert result["already_shared"] is False

    async with session_scope() as session:
        share_list = await shares.list_shares(session, owner_id, artifact_id)
    assert len(share_list) == 1
    assert share_list[0]["email"] == "dashboard-recipient@example.com"

    async with session_scope() as session:
        await shares.unshare_artifact(session, owner_id, artifact_id, share_list[0]["share_id"])

    async with session_scope() as session:
        assert await shares.list_shares(session, owner_id, artifact_id) == []


async def test_list_shares_by_non_owner_raises():
    owner_id = await make_user(email="list-owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)
    other_user_id = await make_user(email="list-not-owner@example.com")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await shares.list_shares(session, other_user_id, artifact_id)


async def test_share_from_dashboard_by_non_owner_raises():
    owner_id = await make_user(email="real-owner@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)
    other_user_id = await make_user(email="not-the-owner@example.com")
    await make_user(email="target@example.com")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await shares.share_artifact_from_dashboard(session, other_user_id, artifact_id, "target@example.com")


async def test_unshare_by_non_owner_raises():
    owner_id = await make_user(email="owns-it@example.com")
    connection_id = await make_connection(user_id=owner_id)
    artifact_id = await _publish(connection_id)
    recipient_id = await make_user(email="recipient4@example.com")

    async with session_scope() as session:
        await shares.share_artifact_from_dashboard(session, owner_id, artifact_id, "recipient4@example.com")
        share_list = await shares.list_shares(session, owner_id, artifact_id)

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await shares.unshare_artifact(session, recipient_id, artifact_id, share_list[0]["share_id"])
