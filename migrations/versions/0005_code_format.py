"""code format

Revision ID: 0005_code_format
Revises: 0004_oauth
Create Date: 2026-07-26 21:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005_code_format'
down_revision: Union[str, Sequence[str], None] = '0004_oauth'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Purely additive — no batch_alter_table needed. The `format` column
    # itself needs no migration: it's a plain VARCHAR with no CHECK
    # constraint (confirmed by inspecting a migrated DB's sqlite_master),
    # so the new "code" enum value just works once ArtifactFormat has it.
    op.add_column("artifacts", sa.Column("language", sa.String(), nullable=True))
    op.add_column("artifact_versions", sa.Column("language", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("artifact_versions", "language")
    op.drop_column("artifacts", "language")
