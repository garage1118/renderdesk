"""squashed baseline

Collapses migrations 0001-0009 into a single schema-creating revision,
keeping 0010 as a normal migration on top of it.

Revision ID: 0009_oidc_identities
Revises:
Create Date: 2026-08-15 12:00:00.000000

WHY THE SQUASH STOPS AT 0009, AND KEEPS THAT REVISION ID
--------------------------------------------------------
A squash may only absorb revisions that *every* installation has already
applied, and it must keep the id of the newest revision it absorbs, so
that a deployment sitting on that id still resolves and simply has
nothing to re-run.

0009 is that watermark here. Every tag that has ever produced a published
image — v1.0.0-rc1, v1.0.0, v1.1.0-rc1, v1.1.0-rc2, v1.1.0, v1.2.0-rc1,
v1.2.0-rc2 — ships exactly 0001-0009, and migrations run at startup, so
any booted installation of any published image is at
`0009_oidc_identities`. This matters more than it looks: the image is
public on Docker Hub, so the installed base includes hosts that cannot be
inspected or fixed by hand, and it spans two channels moving at different
speeds — `:edge` gets every tagged release including RCs, while `:latest`
only moves on non-RC ones and therefore skips RC-only migrations
entirely.

Squashing any *further* — folding 0010 in too — would break the `:latest`
channel specifically. An installation there upgrading from a pre-0010
release would find its stored revision gone (startup failure, since
alembic cannot resolve `0009_oidc_identities` if this file removes it)
and would never run 0010's byte_size backfill. Stopping at 0009 means
this file and 0010 can ship in the same image, with no `alembic stamp`
and no hand-edited `alembic_version` row on any host.

The residual assumption is that nobody is running a build from an
untagged commit that predates 0009; such a host would need a manual stamp,
which is not something we can perform on someone else's machine. Every
published image is covered.

EQUIVALENCE TO THE CHAIN IT REPLACES
------------------------------------
Verified by building one database from 0001-0009 and one from this file,
then comparing structurally: identical tables, column names, types,
nullability, primary keys, foreign-key targets, uniquely-constrained
column sets and indexed column sets. Three non-semantic differences
survive, all inherent to generating a baseline from the models rather
than replaying DDL:

1. Physical column order — the chain appended `language` (0005) via ALTER
   TABLE, so it sits last on `artifacts` there and in model order here.
   SQLAlchemy addresses columns by name; nothing in the app sees this.
2. Two foreign keys on `connections` are named on a chain-built database
   (`fk_connections_user_id`, `fk_connections_client_id`, from 0002's
   batch_alter_table naming convention) and anonymous here. This is the
   one with a future consequence: a migration dropping or altering either
   *by name* would work on an upgraded host and fail on a fresh install.
   Use batch_alter_table with an explicit naming convention rather than
   assuming either shape.
3. `users.email`, `connections.token_hash` and `sessions.token_hash` are
   uniqueness-enforced twice on a chain-built database (an inline UNIQUE
   constraint *and* the unique index `unique=True, index=True` produces)
   and once here, by the index alone. Same guarantee; the chain carries a
   redundant copy.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0009_oidc_identities'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('oauth_clients',
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('metadata_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('client_id')
    )
    op.create_table('users',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_table('connections',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=True),
    sa.Column('client_id', sa.String(), nullable=True),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_connections_client_id'), 'connections', ['client_id'], unique=False)
    op.create_index(op.f('ix_connections_token_hash'), 'connections', ['token_hash'], unique=True)
    op.create_index(op.f('ix_connections_user_id'), 'connections', ['user_id'], unique=False)
    op.create_table('oauth_authorization_codes',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=True),
    sa.Column('redirect_uri', sa.String(), nullable=False),
    sa.Column('code_challenge', sa.String(), nullable=False),
    sa.Column('scope', sa.String(), nullable=True),
    sa.Column('state', sa.String(), nullable=True),
    sa.Column('code_hash', sa.String(), nullable=True),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        op.f('ix_oauth_authorization_codes_client_id'), 'oauth_authorization_codes', ['client_id'], unique=False
    )
    op.create_index(
        op.f('ix_oauth_authorization_codes_code_hash'), 'oauth_authorization_codes', ['code_hash'], unique=True
    )
    op.create_table('oidc_identities',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('issuer', sa.String(), nullable=False),
    sa.Column('subject', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('issuer', 'subject', name='uq_oidc_identities_issuer_subject')
    )
    op.create_index(op.f('ix_oidc_identities_user_id'), 'oidc_identities', ['user_id'], unique=False)
    op.create_table('sessions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('user_id', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sessions_token_hash'), 'sessions', ['token_hash'], unique=True)
    op.create_index(op.f('ix_sessions_user_id'), 'sessions', ['user_id'], unique=False)
    op.create_table('artifacts',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('connection_id', sa.String(), nullable=False),
    sa.Column('title', sa.String(), nullable=True),
    sa.Column('format', sa.Enum('html', 'markdown', 'code', 'csv', 'react', name='artifactformat'), nullable=False),
    sa.Column('language', sa.String(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('byte_size', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_artifacts_connection_id'), 'artifacts', ['connection_id'], unique=False)
    op.create_table('oauth_access_tokens',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('connection_id', sa.String(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        op.f('ix_oauth_access_tokens_connection_id'), 'oauth_access_tokens', ['connection_id'], unique=False
    )
    op.create_index(op.f('ix_oauth_access_tokens_token_hash'), 'oauth_access_tokens', ['token_hash'], unique=True)
    op.create_table('oauth_refresh_tokens',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('connection_id', sa.String(), nullable=False),
    sa.Column('client_id', sa.String(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['client_id'], ['oauth_clients.client_id'], ),
    sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(
        op.f('ix_oauth_refresh_tokens_connection_id'), 'oauth_refresh_tokens', ['connection_id'], unique=False
    )
    op.create_index(op.f('ix_oauth_refresh_tokens_token_hash'), 'oauth_refresh_tokens', ['token_hash'], unique=True)
    op.create_table('artifact_shares',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('artifact_id', sa.String(), nullable=False),
    sa.Column('shared_with_user_id', sa.String(), nullable=False),
    sa.Column('shared_by_connection_id', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], ),
    sa.ForeignKeyConstraint(['shared_by_connection_id'], ['connections.id'], ),
    sa.ForeignKeyConstraint(['shared_with_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('artifact_id', 'shared_with_user_id')
    )
    op.create_index(op.f('ix_artifact_shares_artifact_id'), 'artifact_shares', ['artifact_id'], unique=False)
    op.create_index(
        op.f('ix_artifact_shares_shared_with_user_id'), 'artifact_shares', ['shared_with_user_id'], unique=False
    )
    op.create_table('artifact_versions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('artifact_id', sa.String(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('format', sa.Enum('html', 'markdown', 'code', 'csv', 'react', name='artifactformat'), nullable=False),
    sa.Column('language', sa.String(), nullable=True),
    sa.Column('title', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('artifact_id', 'version', name='uq_artifact_versions_artifact_id_version')
    )
    op.create_index(op.f('ix_artifact_versions_artifact_id'), 'artifact_versions', ['artifact_id'], unique=False)
    op.create_table('comments',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('artifact_id', sa.String(), nullable=False),
    sa.Column('parent_id', sa.String(), nullable=True),
    sa.Column('author_user_id', sa.String(), nullable=True),
    sa.Column('author_connection_id', sa.String(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('resolved', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifacts.id'], ),
    sa.ForeignKeyConstraint(['author_connection_id'], ['connections.id'], ),
    sa.ForeignKeyConstraint(['author_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['parent_id'], ['comments.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_comments_artifact_id'), 'comments', ['artifact_id'], unique=False)
    op.create_index(op.f('ix_comments_parent_id'), 'comments', ['parent_id'], unique=False)


def downgrade() -> None:
    # Nothing to downgrade to — this is the base revision.
    raise NotImplementedError("0009_oidc_identities is the base revision")
