"""oauth

Revision ID: 0004_oauth
Revises: 0003_artifact_shares
Create Date: 2026-07-26 03:53:56.477199

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004_oauth'
down_revision: Union[str, Sequence[str], None] = '0003_artifact_shares'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), primary_key=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "oauth_authorization_codes",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("client_id", sa.String(), sa.ForeignKey("oauth_clients.client_id"), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("code_hash", sa.String(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_oauth_authorization_codes_client_id", "oauth_authorization_codes", ["client_id"])
    op.create_index(
        "ix_oauth_authorization_codes_code_hash", "oauth_authorization_codes", ["code_hash"], unique=True
    )

    op.create_table(
        "oauth_access_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_oauth_access_tokens_token_hash", "oauth_access_tokens", ["token_hash"], unique=True)
    op.create_index("ix_oauth_access_tokens_connection_id", "oauth_access_tokens", ["connection_id"])

    op.create_table(
        "oauth_refresh_tokens",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("connection_id", sa.String(), sa.ForeignKey("connections.id"), nullable=False),
        sa.Column("client_id", sa.String(), sa.ForeignKey("oauth_clients.client_id"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_oauth_refresh_tokens_token_hash", "oauth_refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_oauth_refresh_tokens_connection_id", "oauth_refresh_tokens", ["connection_id"])

    with op.batch_alter_table("connections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "client_id",
                sa.String(),
                sa.ForeignKey("oauth_clients.client_id", name="fk_connections_client_id"),
                nullable=True,
            )
        )
        batch_op.alter_column("token_hash", nullable=True)
        batch_op.alter_column("expires_at", nullable=True)
        batch_op.create_index("ix_connections_client_id", ["client_id"])


def downgrade() -> None:
    with op.batch_alter_table("connections") as batch_op:
        batch_op.drop_index("ix_connections_client_id")
        batch_op.alter_column("expires_at", nullable=False)
        batch_op.alter_column("token_hash", nullable=False)
        batch_op.drop_column("client_id")

    op.drop_table("oauth_refresh_tokens")
    op.drop_table("oauth_access_tokens")
    op.drop_table("oauth_authorization_codes")
    op.drop_table("oauth_clients")
