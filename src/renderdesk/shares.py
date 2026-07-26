import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.models import Artifact, ArtifactShare, Connection, User
from renderdesk.tools import get_owned_artifact


class RecipientNotFoundError(Exception):
    pass


class SelfShareError(Exception):
    pass


async def share_artifact(session: AsyncSession, connection_id: str, artifact_id: str, email: str) -> dict:
    artifact = await get_owned_artifact(session, connection_id, artifact_id)

    recipient = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if recipient is None:
        raise RecipientNotFoundError(f"not_found: no user with email {email!r}")

    owner_connection = (
        await session.execute(select(Connection).where(Connection.id == artifact.connection_id))
    ).scalar_one()
    if owner_connection.user_id == recipient.id:
        raise SelfShareError("invalid: cannot share an artifact with its own owner")

    existing = (
        await session.execute(
            select(ArtifactShare).where(
                ArtifactShare.artifact_id == artifact_id, ArtifactShare.shared_with_user_id == recipient.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"shared_with": email, "already_shared": True}

    session.add(
        ArtifactShare(
            id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            shared_with_user_id=recipient.id,
            shared_by_connection_id=connection_id,
        )
    )
    await session.commit()
    return {"shared_with": email, "already_shared": False}


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
            "shared_by_connection_label": label,
            "shared_at": shared_at.isoformat(),
        }
        for artifact, shared_at, label in rows
    ]
