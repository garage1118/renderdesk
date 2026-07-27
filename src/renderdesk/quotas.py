import asyncio
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.config import settings
from renderdesk.models import Artifact


class QuotaExceededError(Exception):
    pass


# Guards the read-check-then-write sequence in publish_artifact/
# update_artifact/reassign_artifact_connection against two concurrent
# requests for the *same* connection both passing a quota check before
# either has committed. SQLite's own transaction locking doesn't close this
# window on its own: the quota read happens in a plain SELECT, which
# doesn't block a second concurrent SELECT from also seeing pre-update
# counts before the first request's write commits.
#
# Keyed by connection_id (not a single global lock) so unrelated
# connections/users still run fully in parallel — only two requests
# racing for the *same* connection's quota ever wait on each other. This
# is process-local: correct for this app's single uvicorn worker (see
# Dockerfile), but wouldn't coordinate across multiple worker processes if
# this were ever scaled out horizontally.
_connection_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def quota_lock(connection_id: str) -> asyncio.Lock:
    return _connection_locks[connection_id]


async def check_new_artifact_quota(session: AsyncSession, connection_id: str, byte_size: int) -> None:
    if byte_size > settings.max_bytes_per_artifact:
        raise QuotaExceededError(
            f"quota_exceeded: artifact is {byte_size} bytes, "
            f"max is {settings.max_bytes_per_artifact} bytes per artifact"
        )

    count, total_bytes = (
        await session.execute(
            select(func.count(Artifact.id), func.coalesce(func.sum(Artifact.byte_size), 0)).where(
                Artifact.connection_id == connection_id
            )
        )
    ).one()

    if count >= settings.max_artifacts_per_connection:
        raise QuotaExceededError(
            f"quota_exceeded: connection already has {count} artifacts, "
            f"max is {settings.max_artifacts_per_connection}"
        )

    if total_bytes + byte_size > settings.max_total_bytes_per_connection:
        raise QuotaExceededError(
            f"quota_exceeded: connection total would be {total_bytes + byte_size} bytes, "
            f"max is {settings.max_total_bytes_per_connection} bytes"
        )


async def check_update_quota(
    session: AsyncSession, connection_id: str, artifact_id: str, old_byte_size: int, new_byte_size: int
) -> None:
    if new_byte_size > settings.max_bytes_per_artifact:
        raise QuotaExceededError(
            f"quota_exceeded: artifact is {new_byte_size} bytes, "
            f"max is {settings.max_bytes_per_artifact} bytes per artifact"
        )

    total_bytes = (
        await session.execute(
            select(func.coalesce(func.sum(Artifact.byte_size), 0)).where(
                Artifact.connection_id == connection_id, Artifact.id != artifact_id
            )
        )
    ).scalar_one()

    if total_bytes + new_byte_size > settings.max_total_bytes_per_connection:
        raise QuotaExceededError(
            f"quota_exceeded: connection total would be {total_bytes + new_byte_size} bytes, "
            f"max is {settings.max_total_bytes_per_connection} bytes"
        )
