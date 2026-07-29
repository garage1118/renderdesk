"""password hash nullable

Revision ID: 0008_password_hash_nullable
Revises: 0007_app_settings
Create Date: 2026-07-28 09:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0008_password_hash_nullable'
down_revision: Union[str, Sequence[str], None] = '0007_app_settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # OIDC-provisioned users (see 0009_oidc_identities) never set a
    # password — see session_auth.verify_password for the None-safe check.
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Any OIDC-only users would otherwise violate the restored NOT NULL —
    # give them an empty (never-matching bcrypt) hash rather than fail
    # the downgrade outright.
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE users SET password_hash = '' WHERE password_hash IS NULL"))
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(), nullable=False)
