import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Text, UniqueConstraint
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    password_hash: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    label: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class Session(Base):
    """A logged-in human's dashboard session — distinct from Connection (an MCP
    token), checked by a separate code path so the two can never be confused."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


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


class Comment(Base):
    """Self-referential: parent_id NULL means this row is a thread root, and a
    thread's resolved state lives on its root. Exactly one of author_user_id /
    author_connection_id is set — every comment has a real identity, a human
    or a specific agent connection, never a bare 'human'/'agent' tag."""

    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("comments.id"), default=None, index=True)
    author_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), default=None)
    author_connection_id: Mapped[str | None] = mapped_column(ForeignKey("connections.id"), default=None)
    body: Mapped[str] = mapped_column(Text)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArtifactShare(Base):
    """Sharing is outbound-only from the MCP tool surface (see share_artifact)
    — this table is what the dashboard's 'Shared with you' section reads, but
    the calling connection's own MCP tools never consult it."""

    __tablename__ = "artifact_shares"
    __table_args__ = (UniqueConstraint("artifact_id", "shared_with_user_id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id"), index=True)
    shared_with_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    shared_by_connection_id: Mapped[str] = mapped_column(ForeignKey("connections.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
