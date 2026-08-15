"""artifact version byte size

Revision ID: 0010_artifact_version_byte_size
Revises: 0009_oidc_identities
Create Date: 2026-08-15 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0010_artifact_version_byte_size'
down_revision: Union[str, Sequence[str], None] = '0009_oidc_identities'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ROLLOUT NOTE (per DESIGN_NOTES.md: a data backfill needs one). The
    # column is nullable and additive, so the schema change itself is the
    # no-special-handling kind. The UPDATE below is what makes this worth
    # calling out:
    #
    # - It rewrites every artifact_versions row once, reading each row's
    #   full content to measure it. On the live deployment that's the
    #   single most expensive statement any migration has run so far —
    #   version history is not quota-capped (quotas.py only sums
    #   Artifact.byte_size), so this table is the largest one here.
    # - It runs at app startup inside _run_migrations' retry loop, so a
    #   slow run delays the container becoming healthy rather than failing
    #   it. If it ever grows slow enough to matter, prune version history
    #   from the dashboard first, then deploy.
    # - It is idempotent and safe to re-run: the WHERE clause only touches
    #   rows that are still NULL.
    #
    # Backfilling (rather than leaving legacy rows NULL and computing at
    # read time) is the whole point: the read path this feeds —
    # versions.list_versions — exists to stop loading content, and a NULL
    # fallback would keep loading it for exactly the old rows that made
    # the page slow.
    op.add_column("artifact_versions", sa.Column("byte_size", sa.Integer(), nullable=True))

    # length(CAST(x AS BLOB)) is SQLite's byte length; bare length() on
    # TEXT counts characters, which would understate any non-ASCII content
    # and disagree with Artifact.byte_size (len(content.encode()) in
    # tools.py). SQLite-specific, which is fine in a migration — the app
    # code this backfills for just reads the column.
    op.execute(
        "UPDATE artifact_versions SET byte_size = length(CAST(content AS BLOB)) WHERE byte_size IS NULL"
    )


def downgrade() -> None:
    op.drop_column("artifact_versions", "byte_size")
