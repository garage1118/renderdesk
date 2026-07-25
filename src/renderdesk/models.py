import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from renderdesk.db import Base


def utcnow() -> datetime:
    # Naive UTC: SQLite has no timezone-aware column type, so the whole app
    # standardizes on naive-but-always-UTC datetimes to avoid aware/naive
    # comparison bugs when values round-trip through the DB.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ArtifactFormat(str, enum.Enum):
    html = "html"
    markdown = "markdown"


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    label: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(primary_key=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"), index=True)
    title: Mapped[str | None] = mapped_column(default=None)
    format: Mapped[ArtifactFormat] = mapped_column(Enum(ArtifactFormat))
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    versions: Mapped[list["ArtifactVersion"]] = relationship(
        back_populates="artifact", cascade="all, delete-orphan"
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    format: Mapped[ArtifactFormat] = mapped_column(Enum(ArtifactFormat))
    title: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    artifact: Mapped[Artifact] = relationship(back_populates="versions")
