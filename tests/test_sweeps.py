from datetime import timedelta

from sqlalchemy import select

from renderdesk.db import session_scope
from renderdesk.models import Session, User, utcnow
from renderdesk.session_auth import create_session, sweep_expired_sessions

from .conftest import make_user


async def _user() -> User:
    user_id = await make_user()
    async with session_scope() as session:
        return (await session.execute(select(User).where(User.id == user_id))).scalar_one()


async def _session_count() -> int:
    async with session_scope() as session:
        return len((await session.execute(select(Session))).scalars().all())


async def test_sweep_deletes_expired_sessions_but_keeps_live_ones():
    # resolve_session only cleans up a row when someone presents that exact
    # token; a session nobody returns to is otherwise never reconsidered.
    user = await _user()
    async with session_scope() as session:
        await create_session(session, user)
        expired = Session(
            id="expired-1",
            user_id=user.id,
            token_hash="stale-hash",
            expires_at=utcnow() - timedelta(days=1),
        )
        session.add(expired)
        await session.commit()

    assert await _session_count() == 2
    removed = await sweep_expired_sessions()
    assert removed == 1
    assert await _session_count() == 1

    async with session_scope() as session:
        remaining = (await session.execute(select(Session))).scalars().all()
    assert remaining[0].id != "expired-1"


async def test_sweep_is_a_no_op_when_nothing_has_expired():
    user = await _user()
    async with session_scope() as session:
        await create_session(session, user)

    assert await sweep_expired_sessions() == 0
    assert await _session_count() == 1
