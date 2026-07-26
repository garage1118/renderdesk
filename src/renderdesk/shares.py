import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.models import Artifact, ArtifactShare, Connection, User
from renderdesk.tools import NotFoundError, get_owned_artifact, get_owned_artifact_by_user


class RecipientNotFoundError(Exception):
    pass


class SelfShareError(Exception):
    pass


async def _create_share(
    session: AsyncSession, artifact: Artifact, owner_user_id: str, shared_by_connection_id: str, email: str
) -> dict:
    recipient = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if recipient is None:
        raise RecipientNotFoundError(f"not_found: no user with email {email!r}")

    if owner_user_id == recipient.id:
        raise SelfShareError("invalid: cannot share an artifact with its own owner")

    existing = (
        await session.execute(
            select(ArtifactShare).where(
                ArtifactShare.artifact_id == artifact.id, ArtifactShare.shared_with_user_id == recipient.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"shared_with": email, "already_shared": True}

    session.add(
        ArtifactShare(
            id=str(uuid.uuid4()),
            artifact_id=artifact.id,
            shared_with_user_id=recipient.id,
            shared_by_connection_id=shared_by_connection_id,
        )
    )
    await session.commit()
    return {"shared_with": email, "already_shared": False}


# --- MCP-facing: ownership checked via the calling connection ---------------


async def share_artifact(session: AsyncSession, connection_id: str, artifact_id: str, email: str) -> dict:
    artifact = await get_owned_artifact(session, connection_id, artifact_id)
    owner_connection = (
        await session.execute(select(Connection).where(Connection.id == artifact.connection_id))
    ).scalar_one()
    return await _create_share(session, artifact, owner_connection.user_id, connection_id, email)


# --- Dashboard-facing: ownership checked via the logged-in user's own
# --- connections, mirroring comments.py's MCP/dashboard split. A share
# --- created here is attributed to the artifact's own owning connection —
# --- there's no separate "connection" representing a human dashboard click.


async def share_artifact_from_dashboard(session: AsyncSession, user_id: str, artifact_id: str, email: str) -> dict:
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    return await _create_share(session, artifact, user_id, artifact.connection_id, email)


async def list_shares(session: AsyncSession, artifact_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(ArtifactShare, User.email)
            .join(User, ArtifactShare.shared_with_user_id == User.id)
            .where(ArtifactShare.artifact_id == artifact_id)
            .order_by(ArtifactShare.created_at.desc())
        )
    ).all()
    return [
        {"share_id": share.id, "email": email, "created_at": share.created_at.isoformat()}
        for share, email in rows
    ]


async def unshare_artifact(session: AsyncSession, user_id: str, artifact_id: str, share_id: str) -> None:
    # Ownership check first — only the owner can revoke a share, not the
    # recipient — then scope the delete to (artifact_id, share_id) so a
    # share_id for a *different* artifact can't be used to guess/hit this.
    artifact = await get_owned_artifact_by_user(session, user_id, artifact_id)
    result = await session.execute(
        delete(ArtifactShare).where(ArtifactShare.id == share_id, ArtifactShare.artifact_id == artifact.id)
    )
    await session.commit()
    if result.rowcount == 0:
        raise NotFoundError(f"not_found: no share {share_id} on artifact {artifact_id}")


async def list_shared_with_user(session: AsyncSession, user_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(Artifact, ArtifactShare.created_at, Connection.label)
            .join(ArtifactShare, ArtifactShare.artifact_id == Artifact.id)
            .join(Connection, Artifact.connection_id == Connection.id)
            .where(ArtifactShare.shared_with_user_id == user_id)
            .order_by(ArtifactShare.created_at.desc())
        )
    ).all()
    return [
        {
            "artifact_id": artifact.id,
            "title": artifact.title,
            "format": artifact.format.value,
            "language": artifact.language,
            "shared_by_connection_label": label,
            "shared_at": shared_at.isoformat(),
        }
        for artifact, shared_at, label in rows
    ]
