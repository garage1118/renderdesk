import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.config import settings
from renderdesk.models import Artifact, ArtifactFormat, ArtifactShare, ArtifactVersion, Comment, Connection
from renderdesk.quotas import QuotaExceededError, check_new_artifact_quota, check_update_quota, quota_lock


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    pass


class InvalidTargetConnectionError(Exception):
    pass


def _parse_format(format: str) -> ArtifactFormat:
    try:
        return ArtifactFormat(format)
    except ValueError:
        raise ValueError(f"invalid format {format!r}: must be 'html', 'markdown', or 'code'") from None


def _artifact_url(artifact_id: str) -> str:
    return f"{settings.public_base_url}/a/{artifact_id}"


async def get_owned_artifact(session: AsyncSession, connection_id: str, artifact_id: str) -> Artifact:
    result = await session.execute(
        select(Artifact).where(Artifact.id == artifact_id, Artifact.connection_id == connection_id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise NotFoundError(f"not_found: no artifact {artifact_id}")
    return artifact


async def get_owned_artifact_by_user(session: AsyncSession, user_id: str, artifact_id: str) -> Artifact:
    """Same ownership rule as get_owned_artifact, but for the dashboard where
    the caller is a human user (who may have several connections) rather
    than a single MCP connection. Deliberately stricter than comments.py's
    get_accessible_artifact — a share grants viewing/commenting, never the
    right to delete someone else's artifact."""
    result = await session.execute(
        select(Artifact)
        .join(Connection, Artifact.connection_id == Connection.id)
        .where(Artifact.id == artifact_id, Connection.user_id == user_id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise NotFoundError(f"not_found: no artifact {artifact_id}")
    return artifact


async def delete_artifact(session: AsyncSession, user_id: str, artifact_id: str) -> None:
    """Dashboard-only — there's no MCP-facing delete tool. Comments and
    shares don't have an ORM cascade defined against Artifact (unlike
    ArtifactVersion), so they're cleared explicitly before the artifact row
    goes; SQLite doesn't enforce FK constraints here, but leaving orphaned
    rows behind would still be wrong."""
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    await session.execute(delete(Comment).where(Comment.artifact_id == artifact_id))
    await session.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == artifact_id))
    await session.delete(artifact)
    await session.commit()


async def reassign_artifact_connection(
    session: AsyncSession, user_id: str, artifact_id: str, target_connection_id: str
) -> dict:
    """Dashboard-only, like delete_artifact — never exposed as an MCP tool,
    since letting a connection request its own artifacts be handed to
    another connection would undo the point of scoping ownership per
    connection. Only ArtifactVersion/Comment authorship is left alone;
    those are a historical record, not a live ownership pointer."""
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)

    if target_connection_id == artifact.connection_id:
        raise InvalidTargetConnectionError("invalid: artifact is already owned by that connection")

    target = (
        await session.execute(
            select(Connection).where(
                Connection.id == target_connection_id,
                Connection.user_id == user_id,
                Connection.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise InvalidTargetConnectionError("not_found: no active connection with that id")

    # Treat the move like publishing a byte_size-sized artifact onto the
    # target, so reassignment can't be used to bypass per-connection quotas.
    # Locked on the *target* connection — it's the one whose quota this
    # check protects, and it's what a concurrent publish/update/reassign
    # onto that same connection would also lock on.
    async with quota_lock(target_connection_id):
        await check_new_artifact_quota(session, target_connection_id, artifact.byte_size)
        artifact.connection_id = target_connection_id
        await session.commit()
    return {"artifact_id": artifact.id, "connection_id": target_connection_id}


async def publish_artifact(
    session: AsyncSession,
    connection_id: str,
    content: str,
    format: str,
    title: str | None = None,
    language: str | None = None,
) -> dict:
    parsed_format = _parse_format(format)
    # UTF-8 encoding never shrinks a string, so this character-count check
    # is a safe, cheap upper bound that rejects oversized content before
    # ever materializing the (potentially much larger) encoded bytes.
    if len(content) > settings.max_bytes_per_artifact:
        raise QuotaExceededError(
            f"quota_exceeded: artifact exceeds {settings.max_bytes_per_artifact} bytes per artifact"
        )
    byte_size = len(content.encode())

    async with quota_lock(connection_id):
        await check_new_artifact_quota(session, connection_id, byte_size)

        artifact_id = str(uuid.uuid4())
        artifact = Artifact(
            id=artifact_id,
            connection_id=connection_id,
            title=title,
            format=parsed_format,
            language=language,
            content=content,
            version=1,
            byte_size=byte_size,
        )
        session.add(artifact)
        session.add(
            ArtifactVersion(
                artifact_id=artifact_id,
                version=1,
                content=content,
                format=parsed_format,
                language=language,
                title=title,
            )
        )
        await session.commit()

    return {"artifact_id": artifact_id, "version": 1, "url": _artifact_url(artifact_id)}


async def update_artifact(
    session: AsyncSession,
    connection_id: str,
    artifact_id: str,
    content: str,
    base_version: int,
    format: str | None = None,
    title: str | None = None,
    language: str | None = None,
) -> dict:
    if len(content) > settings.max_bytes_per_artifact:
        raise QuotaExceededError(
            f"quota_exceeded: artifact exceeds {settings.max_bytes_per_artifact} bytes per artifact"
        )
    byte_size = len(content.encode())

    # Locked from the version check through the commit: besides closing the
    # same quota TOCTOU as publish_artifact, this also makes the
    # base_version optimistic-concurrency check race-free — two concurrent
    # updates against the same artifact can no longer both read the same
    # current version and both "win".
    async with quota_lock(connection_id):
        artifact = await get_owned_artifact(session, connection_id, artifact_id)

        if artifact.version != base_version:
            raise VersionConflictError(
                f"version_conflict: base_version {base_version} is stale, current version is {artifact.version}"
            )

        new_format = _parse_format(format) if format is not None else artifact.format
        await check_update_quota(session, connection_id, artifact_id, artifact.byte_size, byte_size)

        artifact.content = content
        artifact.format = new_format
        artifact.byte_size = byte_size
        if title is not None:
            artifact.title = title
        if language is not None:
            artifact.language = language
        artifact.version += 1

        session.add(
            ArtifactVersion(
                artifact_id=artifact_id,
                version=artifact.version,
                content=content,
                format=new_format,
                language=artifact.language,
                title=artifact.title,
            )
        )
        await session.commit()

    return {"version": artifact.version, "url": _artifact_url(artifact_id)}


async def get_artifact(
    session: AsyncSession,
    connection_id: str,
    artifact_id: str,
    include_content: bool = False,
) -> dict:
    artifact = await get_owned_artifact(session, connection_id, artifact_id)

    result = {
        "artifact_id": artifact.id,
        "title": artifact.title,
        "format": artifact.format.value,
        "language": artifact.language,
        "version": artifact.version,
        "byte_size": artifact.byte_size,
        "url": _artifact_url(artifact.id),
        "created_at": artifact.created_at.isoformat(),
        "updated_at": artifact.updated_at.isoformat(),
    }
    if include_content:
        result["content"] = artifact.content
    return result


async def list_artifacts(
    session: AsyncSession, connection_id: str, limit: int = 50, offset: int = 0
) -> list[dict]:
    result = await session.execute(
        select(Artifact)
        .where(Artifact.connection_id == connection_id)
        .order_by(Artifact.updated_at.desc())
        .limit(min(limit, 200))
        .offset(max(offset, 0))
    )
    artifacts = result.scalars().all()
    return [
        {
            "artifact_id": a.id,
            "title": a.title,
            "format": a.format.value,
            "language": a.language,
            "version": a.version,
            "byte_size": a.byte_size,
            "url": _artifact_url(a.id),
            "updated_at": a.updated_at.isoformat(),
        }
        for a in artifacts
    ]
