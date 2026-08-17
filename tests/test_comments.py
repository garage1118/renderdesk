import pytest
from sqlalchemy import select

from renderdesk import comments, shares, tools
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.models import Connection, User
from renderdesk.quotas import QuotaExceededError

from .conftest import make_connection, make_user


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
    assert resolved[0]["comments"][0]["author"] == user.email
    assert resolved[0]["comments"][0]["author_kind"] == "human"


async def test_thread_distinguishes_each_author_not_just_human_vs_agent():
    # The case the per-connection identity model exists for: two different
    # agent connections plus a human in one thread. Before authorship was
    # surfaced these were three comments labelled "agent"/"agent"/"human",
    # with no way for a reader — or an agent reading its own thread — to
    # tell which agent said what.
    owner = await make_user(email="threads@example.com")
    first = await make_connection(label="claude-code", user_id=owner)
    second = await make_connection(label="vscode", user_id=owner)
    artifact_id = await _publish(first)
    user = await _user_for(first)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "please review")
    async with session_scope() as session:
        await comments.reply_to_comment(session, first, root["thread_id"], "looking")
    # The second connection can only reply to a thread on an artifact it
    # owns, so hand it the artifact first.
    async with session_scope() as session:
        await tools.reassign_artifact_connection(session, owner, artifact_id, second)
    async with session_scope() as session:
        await comments.reply_to_comment(session, second, root["thread_id"], "done")

    async with session_scope() as session:
        threads = await comments.list_comments(session, second, artifact_id)

    assert [(c["author"], c["author_kind"]) for c in threads[0]["comments"]] == [
        ("threads@example.com", "human"),
        ("claude-code", "agent"),
        ("vscode", "agent"),
    ]


async def test_unlabelled_connection_falls_back_to_a_stable_non_empty_name():
    # label is nullable, and author[0] is rendered as an avatar initial in
    # the dashboard — an empty or None author would break that page.
    connection_id = await make_connection(label=None)
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "please review")
    async with session_scope() as session:
        reply = await comments.reply_to_comment(session, connection_id, root["thread_id"], "ok")

    assert reply["author"] == f"connection {connection_id[:8]}"
    assert reply["author_kind"] == "agent"


async def test_comment_count_quota_is_enforced_per_artifact(monkeypatch):
    # Regression for CLAUDE-SECURITY-RESULTS.md F19: comments were counted
    # against no quota at all — an owner or a share recipient could write
    # unbounded rows against one artifact.
    monkeypatch.setattr(settings, "max_comments_per_artifact", 2)
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        await comments.create_comment(session, user, artifact_id, "one")
    async with session_scope() as session:
        await comments.create_comment(session, user, artifact_id, "two")
    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await comments.create_comment(session, user, artifact_id, "three")


async def test_comment_byte_quota_is_enforced_per_artifact(monkeypatch):
    monkeypatch.setattr(settings, "max_comment_bytes_per_artifact", 10)
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await comments.create_comment(session, user, artifact_id, "way too long for the byte cap")


async def test_share_recipient_comments_still_count_against_the_artifact_quota(monkeypatch):
    # The quota is per-artifact, not per-writer, specifically because a
    # share recipient — who never owns the artifact — can also write
    # comments against it.
    monkeypatch.setattr(settings, "max_comments_per_artifact", 1)
    owner_connection = await make_connection(label="owner-conn")
    artifact_id = await _publish(owner_connection)
    owner = await _user_for(owner_connection)
    recipient_id = await make_user(email="recipient@example.com")

    async with session_scope() as session:
        await shares.share_artifact_from_dashboard(session, owner.id, artifact_id, "recipient@example.com")
    async with session_scope() as session:
        recipient = (await session.execute(select(User).where(User.id == recipient_id))).scalar_one()

    async with session_scope() as session:
        await comments.create_comment(session, recipient, artifact_id, "from recipient")
    async with session_scope() as session:
        with pytest.raises(QuotaExceededError):
            await comments.create_comment(session, owner, artifact_id, "from owner, over quota")


async def test_dashboard_comment_listing_is_capped(monkeypatch):
    monkeypatch.setattr(comments, "MAX_DASHBOARD_THREADS", 3)
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    for i in range(5):
        async with session_scope() as session:
            await comments.create_comment(session, user, artifact_id, f"comment {i}")

    async with session_scope() as session:
        threads = await comments.list_comments_for_dashboard(session, user, artifact_id)
    assert len(threads) == 3


async def test_owner_can_delete_a_comment_thread():
    connection_id = await make_connection()
    artifact_id = await _publish(connection_id)
    user = await _user_for(connection_id)

    async with session_scope() as session:
        root = await comments.create_comment(session, user, artifact_id, "delete me")
        await comments.reply_to_comment(session, connection_id, root["thread_id"], "a reply")

    async with session_scope() as session:
        await comments.delete_thread(session, user.id, artifact_id, root["thread_id"])

    async with session_scope() as session:
        threads = await comments.list_comments_for_dashboard(session, user, artifact_id)
    assert threads == []


async def test_non_owner_cannot_delete_a_comment_thread():
    owner_connection = await make_connection(label="owner-conn2")
    artifact_id = await _publish(owner_connection)
    owner = await _user_for(owner_connection)
    recipient_id = await make_user(email="recipient2@example.com")

    async with session_scope() as session:
        await shares.share_artifact_from_dashboard(session, owner.id, artifact_id, "recipient2@example.com")
    async with session_scope() as session:
        owner_row = await comments.create_comment(session, owner, artifact_id, "owner's comment")

    async with session_scope() as session:
        with pytest.raises(tools.NotFoundError):
            await comments.delete_thread(session, recipient_id, artifact_id, owner_row["thread_id"])
