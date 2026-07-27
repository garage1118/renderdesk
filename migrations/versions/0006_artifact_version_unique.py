"""artifact version unique constraint

Revision ID: 0006_artifact_version_unique
Revises: 0005_code_format
Create Date: 2026-07-27 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0006_artifact_version_unique'
down_revision: Union[str, Sequence[str], None] = '0005_code_format'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive: a concurrent update_artifact TOCTOU race (documented, not
    # yet fixed — see the code review) could in theory have already produced
    # duplicate (artifact_id, version) rows before this constraint existed.
    # If so, adding the constraint below would fail outright and take the
    # whole app down at the next deploy. Clear out any older duplicates
    # first (keeping the highest id, i.e. the most recently written row) —
    # a no-op on the expected case of no duplicates.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM artifact_versions WHERE id NOT IN "
            "(SELECT MAX(id) FROM artifact_versions GROUP BY artifact_id, version)"
        )
    )
    with op.batch_alter_table("artifact_versions") as batch_op:
        batch_op.create_unique_constraint(
            "uq_artifact_versions_artifact_id_version", ["artifact_id", "version"]
        )


def downgrade() -> None:
    with op.batch_alter_table("artifact_versions") as batch_op:
        batch_op.drop_constraint("uq_artifact_versions_artifact_id_version", type_="unique")
