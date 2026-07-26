"""artifact shares

Revision ID: 0003_artifact_shares
Revises: 0002_users_sessions_comments
Create Date: 2026-07-25 22:29:36.397957

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003_artifact_shares'
down_revision: Union[str, Sequence[str], None] = '0002_users_sessions_comments'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "artifact_shares",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("artifact_id", sa.String(), sa.ForeignKey("artifacts.id"), nullable=False),
        sa.Column("shared_with_user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("shared_by_connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("artifact_id", "shared_with_user_id"),
    )
    op.create_index("ix_artifact_shares_artifact_id", "artifact_shares", ["artifact_id"])
    op.create_index("ix_artifact_shares_shared_with_user_id", "artifact_shares", ["shared_with_user_id"])


def downgrade() -> None:
    op.drop_table("artifact_shares")
