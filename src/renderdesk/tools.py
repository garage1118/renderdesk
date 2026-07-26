import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.config import settings
from renderdesk.models import Artifact, ArtifactFormat, ArtifactVersion
from renderdesk.quotas import check_new_artifact_quota, check_update_quota


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    pass


def _parse_format(format: str) -> ArtifactFormat:
    try:
        return ArtifactFormat(format)
    except ValueError:
        raise ValueError(f"invalid format {format!r}: must be 'html' or 'markdown'") from None


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


async def publish_artifact(
    session: AsyncSession,
    connection_id: str,
    content: str,
    format: str,
    title: str | None = None,
) -> dict:
    parsed_format = _parse_format(format)
    byte_size = len(content.encode())
    await check_new_artifact_quota(session, connection_id, byte_size)

    artifact_id = str(uuid.uuid4())
    artifact = Artifact(
        id=artifact_id,
        connection_id=connection_id,
        title=title,
        format=parsed_format,
        content=content,
        version=1,
        byte_size=byte_size,
    )
    session.add(artifact)
    session.add(
        ArtifactVersion(
            artifact_id=artifact_id, version=1, content=content, format=parsed_format, title=title
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
) -> dict:
    artifact = await get_owned_artifact(session, connection_id, artifact_id)

    if artifact.version != base_version:
        raise VersionConflictError(
            f"version_conflict: base_version {base_version} is stale, current version is {artifact.version}"
        )

    new_format = _parse_format(format) if format is not None else artifact.format
    byte_size = len(content.encode())
    await check_update_quota(session, connection_id, artifact_id, artifact.byte_size, byte_size)

    artifact.content = content
    artifact.format = new_format
    artifact.byte_size = byte_size
    if title is not None:
        artifact.title = title
    artifact.version += 1

    session.add(
        ArtifactVersion(
            artifact_id=artifact_id,
            version=artifact.version,
            content=content,
            format=new_format,
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
        "version": artifact.version,
        "byte_size": artifact.byte_size,
        "url": _artifact_url(artifact.id),
        "created_at": artifact.created_at.isoformat(),
        "updated_at": artifact.updated_at.isoformat(),
    }
    if include_content:
        result["content"] = artifact.content
    return result


async def list_artifacts(session: AsyncSession, connection_id: str, limit: int = 50) -> list[dict]:
    result = await session.execute(
        select(Artifact)
        .where(Artifact.connection_id == connection_id)
        .order_by(Artifact.updated_at.desc())
        .limit(limit)
    )
    artifacts = result.scalars().all()
    return [
        {
            "artifact_id": a.id,
            "title": a.title,
            "format": a.format.value,
            "version": a.version,
            "byte_size": a.byte_size,
            "url": _artifact_url(a.id),
            "updated_at": a.updated_at.isoformat(),
        }
        for a in artifacts
    ]
