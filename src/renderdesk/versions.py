from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.models import ArtifactVersion
from renderdesk.tools import NotFoundError, get_owned_artifact_by_user

# Dashboard-only, like delete_artifact/reassign_artifact_connection — an
# agent has no legitimate need to browse or prune history, and this is
# squarely a human storage-management concern, so none of this is exposed
# as an MCP tool.


class CannotDeleteCurrentVersionError(Exception):
    pass


async def list_versions(session: AsyncSession, user_id: str, artifact_id: str) -> list[dict]:
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    rows = (
        await session.execute(
            select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version": v.version,
            "title": v.title,
            "format": v.format.value,
            "language": v.language,
            "byte_size": len(v.content.encode()),
            "created_at": v.created_at,
            "is_current": v.version == artifact.version,
        }
        for v in rows
    ]


async def get_version(session: AsyncSession, user_id: str, artifact_id: str, version: int) -> ArtifactVersion:
    await get_owned_artifact_by_user(session, user_id, artifact_id)
    row = (
        await session.execute(
            select(ArtifactVersion).where(
                ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == version
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"not_found: no version {version} for artifact {artifact_id}")
    return row


async def delete_version(session: AsyncSession, user_id: str, artifact_id: str, version: int) -> None:
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    if version == artifact.version:
        # The current version's row is the one thing tying "the live
        # artifact" to "a browsable snapshot of it" — keep it, so history
        # never looks like it's missing the state you're looking at right
        # now. Superseded versions are fair game.
        raise CannotDeleteCurrentVersionError("invalid: cannot delete the current version")

    result = await session.execute(
        delete(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version == version
        )
    )
    await session.commit()
    if result.rowcount == 0:
        raise NotFoundError(f"not_found: no version {version} for artifact {artifact_id}")


async def prune_old_versions(session: AsyncSession, user_id: str, artifact_id: str) -> int:
    """Deletes every version row except the current one. Returns the count
    of rows removed, for a confirmation message."""
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    result = await session.execute(
        delete(ArtifactVersion).where(
            ArtifactVersion.artifact_id == artifact_id, ArtifactVersion.version != artifact.version
        )
    )
    await session.commit()
    return result.rowcount
