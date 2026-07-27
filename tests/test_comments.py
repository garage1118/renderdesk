import pytest
from sqlalchemy import select

from renderdesk import comments, tools
from renderdesk.db import session_scope
from renderdesk.models import Connection, User

from .conftest import make_connection


async def _publish(connection_id: str) -> str:
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "hello", "markdown")
    return published["artifact_id"]


async def _user_for(connection_id: str) -> User:
    async with session_scope() as session:
        connection = (
            await session.execute(select(Connection).where(Connection.id == connection_id))
        ).scalar_one()
        return (await session.execute(select(User).where(User.id == connection.user_id))).scalar_one()


async def test_reply_and_list_round_trip():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)

    async with session_scope() as session:
        thread = await comments.list_comments(session, connection_id, artifact_id)
    assert thread == []


async def test_thread_visible_only_to_owning_connection():
    connection_a = await make_connection("a")
    connection_b = await make_connection("b")
    artifact_id = await _publish(connection_a)

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await comments.list_comments(session, connection_b, artifact_id)

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await comments.reply_to_comment(session, connection_b, "does-not-matter", "hi")


async def test_reply_to_non_root_comment_is_rejected():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "first comment")

    async with session_scope() as session:
        reply = await comments.reply_to_comment(session, connection_id, root["thread_id"], "agent reply")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            # replying to a reply (not a thread root) must be rejected
            await comments.reply_to_comment(session, connection_id, reply["comment_id"], "nested reply")


async def test_list_comments_groups_replies_onto_the_right_thread():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        thread_a = await comments.create_comment(session, user, artifact_id, "thread a")
    async with session_scope() as session:
        thread_b = await comments.create_comment(session, user, artifact_id, "thread b")
    async with session_scope() as session:
        await comments.reply_to_comment(session, connection_id, thread_a["thread_id"], "reply on a")

    async with session_scope() as session:
        threads = await comments.list_comments(session, connection_id, artifact_id)

    by_id = {t["thread_id"]: t for t in threads}
    assert [c["body"] for c in by_id[thread_a["thread_id"]]["comments"]] == ["thread a", "reply on a"]
    assert [c["body"] for c in by_id[thread_b["thread_id"]]["comments"]] == ["thread b"]


async def test_oversized_comment_body_is_rejected():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)
    huge_body = "x" * (comments.MAX_COMMENT_BYTES + 1)

    async with session_scope() as session:
        with pytest.raises(comments.CommentTooLargeError):
            await comments.create_comment(session, user, artifact_id, huge_body)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "normal-size comment")

    async with session_scope() as session:
        with pytest.raises(comments.CommentTooLargeError):
            await comments.reply_to_comment(session, connection_id, root["thread_id"], huge_body)

    async with session_scope() as session:
        with pytest.raises(comments.CommentTooLargeError):
            await comments.reply_as_human(session, user, root["thread_id"], huge_body)


async def test_resolve_hides_thread_unless_included():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "please review")

    async with session_scope() as session:
        await comments.resolve_comment_thread(session, connection_id, root["thread_id"])

    async with session_scope() as session:
        assert await comments.list_comments(session, connection_id, artifact_id, include_resolved=False) == []

    async with session_scope() as session:
        resolved = await comments.list_comments(session, connection_id, artifact_id, include_resolved=True)
    assert len(resolved) == 1
    assert resolved[0]["resolved"] is True
    assert resolved[0]["comments"][0]["author"] == "human"
