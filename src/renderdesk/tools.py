import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from renderdesk.config import settings
from renderdesk.models import Artifact, ArtifactFormat, ArtifactShare, ArtifactVersion, Comment, Connection
from renderdesk.models import OAuthAccessToken as OAuthAccessTokenRow
from renderdesk.models import OAuthRefreshToken as OAuthRefreshTokenRow
from renderdesk.quotas import QuotaExceededError, check_new_artifact_quota, check_update_quota, quota_lock

# title/language were previously written unbounded and unmeasured — quotas
# only ever accounted for content bytes, so an oversized title could write
# far more than its 1-byte `content='x'` would suggest. Small, fixed caps
# rather than settings:
# these are display metadata, not a deployment-tunable storage budget.
MAX_TITLE_BYTES = 500
MAX_LANGUAGE_BYTES = 100


def _check_metadata_size(title: str | None, language: str | None) -> None:
    # Character count is a safe cheap upper bound on encoded byte size
    # (UTF-8 never shrinks a string), same pattern as the content-size
    # check below — rejects before ever encoding.
    if title is not None and len(title) > MAX_TITLE_BYTES:
        raise QuotaExceededError(f"quota_exceeded: title exceeds {MAX_TITLE_BYTES} bytes")
    if language is not None and len(language) > MAX_LANGUAGE_BYTES:
        raise QuotaExceededError(f"quota_exceeded: language exceeds {MAX_LANGUAGE_BYTES} bytes")


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    pass


class InvalidTargetConnectionError(Exception):
    pass


class ConnectionNotRevokedError(Exception):
    pass


class ConnectionHasHistoryError(Exception):
    pass


def _parse_format(format: str) -> ArtifactFormat:
    try:
        return ArtifactFormat(format)
    except ValueError:
        raise ValueError(
            f"invalid format {format!r}: must be 'html', 'markdown', 'code', 'csv', or 'react'"
        ) from None


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
    right to delete someone else's artifact.

    content is deferred: every caller here wants ownership plus metadata
    (version, byte_size, connection_id) — none reads the body, and the
    dashboard's historical viewer reads ArtifactVersion.content instead.
    Deliberately not done in get_owned_artifact above, which backs
    get_artifact(include_content=True)."""
    result = await session.execute(
        select(Artifact)
        .options(defer(Artifact.content))
        .join(Connection, Artifact.connection_id == Connection.id)
        .where(Artifact.id == artifact_id, Connection.user_id == user_id)
    )
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise NotFoundError(f"not_found: no artifact {artifact_id}")
    return artifact


async def _delete_artifact_rows(session: AsyncSession, artifact: Artifact) -> None:
    """Shared by delete_artifact/admin_delete_artifact. Comments and shares
    don't have an ORM cascade defined against Artifact (unlike
    ArtifactVersion, which does and is handled by session.delete below), so
    they're cleared explicitly before the artifact row goes — PRAGMA
    foreign_keys is enforced (see db.py), so this order matters."""
    await session.execute(delete(Comment).where(Comment.artifact_id == artifact.id))
    await session.execute(delete(ArtifactShare).where(ArtifactShare.artifact_id == artifact.id))
    await session.delete(artifact)
    await session.commit()


async def delete_artifact(session: AsyncSession, user_id: str, artifact_id: str) -> None:
    """Dashboard-only — there's no MCP-facing delete tool. See
    admin_delete_artifact for the CLI's unscoped equivalent."""
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    await _delete_artifact_rows(session, artifact)


async def admin_delete_artifact(session: AsyncSession, artifact_id: str) -> Artifact:
    """CLI-only, deliberately unscoped by owner — shell access to the
    deployed container is already the same trust boundary create-user/
    create-token rely on, so there's no ownership check here the way
    delete_artifact enforces for the dashboard. Never exposed as an MCP
    tool or a dashboard route. Returns the artifact as it was immediately
    before deletion (readable post-commit: session_scope's sessionmaker
    has expire_on_commit=False) so the caller can report what was removed."""
    result = await session.execute(select(Artifact).where(Artifact.id == artifact_id))
    artifact = result.scalar_one_or_none()
    if artifact is None:
        raise NotFoundError(f"not_found: no artifact {artifact_id}")
    await _delete_artifact_rows(session, artifact)
    return artifact


async def admin_delete_connection(session: AsyncSession, connection_id: str) -> Connection:
    """CLI-only, deliberately unscoped by owner — same trust boundary as
    admin_delete_artifact. Mirrors dashboard_delete_connection's guard: only
    a revoked connection can be deleted, and only if it never published an
    artifact, left a comment, or shared anything — a connection is never
    deleted out from under content it created, so that content stays
    around as history rather than vanishing as a side effect of token
    cleanup. Doesn't touch OAuthAuthorizationCode rows (they're already
    swept on expiry — see oauth_provider.sweep_expired_oauth_rows)."""
    connection = (
        await session.execute(select(Connection).where(Connection.id == connection_id))
    ).scalar_one_or_none()
    if connection is None:
        raise NotFoundError(f"not_found: no connection {connection_id}")
    if connection.revoked_at is None:
        raise ConnectionNotRevokedError("invalid: only revoked connections can be deleted")

    artifact_count = (
        await session.execute(select(func.count()).select_from(Artifact).where(Artifact.connection_id == connection_id))
    ).scalar_one()
    comment_count = (
        await session.execute(
            select(func.count()).select_from(Comment).where(Comment.author_connection_id == connection_id)
        )
    ).scalar_one()
    share_count = (
        await session.execute(
            select(func.count())
            .select_from(ArtifactShare)
            .where(ArtifactShare.shared_by_connection_id == connection_id)
        )
    ).scalar_one()
    if artifact_count or comment_count or share_count:
        raise ConnectionHasHistoryError(
            f"invalid: connection published {artifact_count} artifact(s), left {comment_count} "
            f"comment(s), and shared {share_count} item(s) — revoked connections keep their history"
        )

    await session.execute(delete(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.connection_id == connection_id))
    await session.execute(delete(OAuthAccessTokenRow).where(OAuthAccessTokenRow.connection_id == connection_id))
    await session.delete(connection)
    await session.commit()
    return connection


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
    _check_metadata_size(title, language)
    # byte_size backs both the connection quota and reported artifact size,
    # so it has to reflect everything actually written — title/language
    # bytes included, not just content.
    byte_size = len(content.encode()) + len((title or "").encode()) + len((language or "").encode())

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
                byte_size=byte_size,
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
    _check_metadata_size(title, language)

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
        # Resolve title/language to what will actually be stored (an
        # omitted field keeps the artifact's current value) before sizing,
        # so byte_size — and the quota check below — reflect the metadata
        # that's really being written, not just the new content.
        new_title = title if title is not None else artifact.title
        new_language = language if language is not None else artifact.language
        byte_size = len(content.encode()) + len((new_title or "").encode()) + len((new_language or "").encode())
        await check_update_quota(session, connection_id, artifact_id, byte_size)

        artifact.content = content
        artifact.format = new_format
        artifact.byte_size = byte_size
        artifact.title = new_title
        artifact.language = new_language
        artifact.version += 1

        session.add(
            ArtifactVersion(
                artifact_id=artifact_id,
                version=artifact.version,
                content=content,
                byte_size=byte_size,
                format=new_format,
                language=new_language,
                title=new_title,
            )
        )

        # Retention: an update loop against one artifact would otherwise
        # grow artifact_versions without bound even though the connection's
        # accounted total (check_update_quota, above) stays correct — the
        # cap on *count* has to be enforced independently of the cap on
        # *bytes*. Prune inside this same quota_lock-held transaction so a
        # concurrent update against this artifact can't race the count.
        await session.flush()
        version_count = (
            await session.execute(
                select(func.count()).select_from(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)
            )
        ).scalar_one()
        if version_count > settings.max_versions_per_artifact:
            keep_versions = (
                await session.execute(
                    select(ArtifactVersion.version)
                    .where(ArtifactVersion.artifact_id == artifact_id)
                    .order_by(ArtifactVersion.version.desc())
                    .limit(settings.max_versions_per_artifact)
                )
            ).scalars().all()
            await session.execute(
                delete(ArtifactVersion).where(
                    ArtifactVersion.artifact_id == artifact_id,
                    ArtifactVersion.version.not_in(keep_versions),
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
        # This tool returns metadata only (get_artifact is the one that can
        # return content, and only on request) — without the defer, a single
        # call loads up to 200 artifacts' full content to build dicts that
        # discard all of it.
        .options(defer(Artifact.content))
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
