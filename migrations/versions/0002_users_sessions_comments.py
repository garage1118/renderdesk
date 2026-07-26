"""users, sessions, comments; connections.user_id

Revision ID: 0002_users_sessions_comments
Revises: 0001_baseline
Create Date: 2026-07-25 17:38:41.452705

"""
import uuid
from typing import Sequence, Union

import bcrypt
import sqlalchemy as sa
from alembic import op

from renderdesk.config import settings
from renderdesk.models import utcnow

# revision identifiers, used by Alembic.
revision: str = '0002_users_sessions_comments'
down_revision: Union[str, Sequence[str], None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite DDL isn't fully transactional/rolled-back on failure the way a
    # single-statement data change is (each DDL statement effectively commits
    # as it runs), so this validates the one thing that can actually fail —
    # missing bootstrap env vars — before any table is touched. That way a
    # failed run leaves the DB exactly as it was, safe to fix and re-run.
    _check_backfill_prerequisite()

    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)

    op.create_table(
        "comments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("artifact_id", sa.String(), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("parent_id", sa.String(), sa.ForeignKey("comments.id"), nullable=True),
        sa.Column("author_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("author_connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_comments_artifact_id", "comments", ["artifact_id"])
    op.create_index("ix_comments_parent_id", "comments", ["parent_id"])

    # connections.user_id starts nullable so existing rows aren't rejected,
    # gets backfilled below, then tightened to NOT NULL. SQLite can't ALTER
    # a constraint in place, so both steps go through batch mode (which
    # recreates the table under the hood).
    with op.batch_alter_table("connections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_id",
                sa.String(),
                sa.ForeignKey("users.id", name="fk_connections_user_id"),
                nullable=True,
            )
        )

    _backfill_existing_connections()

    with op.batch_alter_table("connections") as batch_op:
        batch_op.alter_column("user_id", nullable=False)
        batch_op.create_index("ix_connections_user_id", ["user_id"])


def _check_backfill_prerequisite() -> None:
    bind = op.get_bind()
    unassigned_count = bind.execute(sa.text("SELECT COUNT(*) FROM connections")).scalar_one()

    if unassigned_count == 0:
        return

    if not settings.admin_bootstrap_email or not settings.admin_bootstrap_password:
        raise RuntimeError(
            f"{unassigned_count} existing connection(s) have no owning user. Set "
            "RENDERDESK_ADMIN_BOOTSTRAP_EMAIL and RENDERDESK_ADMIN_BOOTSTRAP_PASSWORD "
            "and re-run this migration to create the user they'll be assigned to."
        )


def _backfill_existing_connections() -> None:
    bind = op.get_bind()
    connections_table = sa.table(
        "connections", sa.column("id", sa.String()), sa.column("user_id", sa.String())
    )
    users_table = sa.table(
        "users",
        sa.column("id", sa.String()),
        sa.column("email", sa.String()),
        sa.column("password_hash", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )

    unassigned_count = bind.execute(
        sa.select(sa.func.count()).select_from(connections_table).where(connections_table.c.user_id.is_(None))
    ).scalar_one()

    if unassigned_count == 0:
        return

    password_hash = bcrypt.hashpw(settings.admin_bootstrap_password.encode(), bcrypt.gensalt()).decode()
    bootstrap_user_id = str(uuid.uuid4())
    bind.execute(
        users_table.insert().values(
            id=bootstrap_user_id,
            email=settings.admin_bootstrap_email,
            password_hash=password_hash,
            created_at=utcnow(),
        )
    )
    bind.execute(
        connections_table.update()
        .where(connections_table.c.user_id.is_(None))
        .values(user_id=bootstrap_user_id)
    )


def downgrade() -> None:
    op.drop_index("ix_connections_user_id", table_name="connections")
    op.drop_column("connections", "user_id")
    op.drop_table("comments")
    op.drop_table("sessions")
    op.drop_table("users")
