"""app settings

Revision ID: 0007_app_settings
Revises: 0006_artifact_version_unique
Create Date: 2026-07-28 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0007_app_settings'
down_revision: Union[str, Sequence[str], None] = '0006_artifact_version_unique'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
