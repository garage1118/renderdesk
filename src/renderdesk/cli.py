import asyncio
import re
import uuid

import bcrypt
import click
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk import tools
from renderdesk.auth_scheme import AUTH_SCHEMES
from renderdesk.auth_scheme import set_auth_scheme as _set_auth_scheme_persisted
from renderdesk.db import session_scope
from renderdesk.models import Artifact, Connection, OidcIdentity, User, utcnow
from renderdesk.oauth_provider import oauth_provider
from renderdesk.tokens import create_personal_token

# Deliberately loose — just enough to catch obvious typos ("not-an-email"),
# not full RFC 5322 compliance. There's no signup flow to protect; accounts
# are only ever created out-of-band by whoever runs this CLI.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.match(email):
        raise click.ClickException(f"{email!r} doesn't look like a valid email address")


async def _create_user(email: str, password: str) -> None:
    async with session_scope() as session:
        existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            raise click.ClickException(f"a user with email {email!r} already exists")

        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        session.add(User(id=str(uuid.uuid4()), email=email, password_hash=password_hash, created_at=utcnow()))
        await session.commit()


async def _create_token(email: str, label: str | None) -> str:
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"no user with email {email!r} — run create-user first")
        return await create_personal_token(session, user.id, label)


@click.group()
def main() -> None:
    pass


@main.command("create-user")
@click.option("--email", required=True, help="Email to log into the web dashboard with")
def create_user(email: str) -> None:
    """Create a human user who can log into the web dashboard."""
    _validate_email(email)
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    if len(password) < 8:
        raise click.ClickException("password must be at least 8 characters")
    if len(password.encode()) > 72:
        # bcrypt 5.x raises ValueError past this length rather than
        # truncating — reject cleanly here instead of letting hashpw crash.
        raise click.ClickException("password must be at most 72 bytes")
    asyncio.run(_create_user(email, password))
    click.echo(f"Created user {email}")


@main.command("create-token")
@click.option("--email", required=True, help="Email of the user this connection belongs to")
@click.option("--label", default=None, help="Optional label to identify this connection (e.g. 'claude-code')")
def create_token(email: str, label: str | None) -> None:
    """Issue a new MCP bearer token for the given user. Equivalent to creating
    one from the dashboard's Connections page while logged in as that user —
    there is no token-issuing MCP tool either way."""
    token = asyncio.run(_create_token(email, label))
    click.echo("Token (save this now, it will not be shown again):")
    click.echo(token)


async def _delete_artifact(artifact_id: str, title: str, email: str) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Artifact, User.email)
                .join(Connection, Artifact.connection_id == Connection.id)
                .join(User, Connection.user_id == User.id)
                .where(Artifact.id == artifact_id)
            )
        ).first()
        if row is None:
            raise click.ClickException(f"no artifact with id {artifact_id!r}")
        artifact, owner_email = row

        # A correctness check, not a confirmation prompt — this command is
        # expected to run non-interactively (typically invoked by an agent,
        # not a human at a terminal), so there's no one to read a printed
        # summary and answer a y/N prompt. Requiring the caller to already
        # know the title and owner catches a wrong/copy-pasted id. The
        # mismatch error deliberately doesn't say which field was wrong or
        # what the right values are — revealing that would turn the check
        # into a guessing oracle instead of a real assertion that the
        # caller already had the right artifact in mind.
        if (artifact.title or "") != title or owner_email != email:
            raise click.ClickException(
                "title/email don't match this artifact — not deleting. If the caller doesn't "
                "have the correct title and owner email, something upstream is wrong; don't guess."
            )

        try:
            await tools.admin_delete_artifact(session, artifact_id)
        except tools.NotFoundError:
            raise click.ClickException("artifact was deleted by something else just now") from None


@main.command("delete-artifact")
@click.option("--id", "artifact_id", required=True, help="Artifact id (see its dashboard URL: /dashboard/a/<id>)")
@click.option("--title", required=True, help="Artifact's exact title — pass \"\" for an untitled artifact")
@click.option("--email", required=True, help="Exact email of the artifact's owner")
def delete_artifact_cmd(artifact_id: str, title: str, email: str) -> None:
    """Permanently delete any artifact by id, bypassing the per-owner
    scoping the dashboard enforces. --title and --email must exactly match
    the artifact's actual title and owner, or nothing is deleted — this is
    a non-interactive correctness check, not a confirmation prompt, since
    this command is expected to run non-interactively (typically invoked
    by an agent). Never exposed as an MCP tool or a dashboard action —
    shell access to the deployed container is the trust boundary here,
    same as create-user/create-token."""
    asyncio.run(_delete_artifact(artifact_id, title, email))
    click.echo("Deleted.")


async def _lookup_connection(session: AsyncSession, connection_id: str) -> tuple[Connection, str]:
    row = (
        await session.execute(
            select(Connection, User.email)
            .join(User, Connection.user_id == User.id)
            .where(Connection.id == connection_id)
        )
    ).first()
    if row is None:
        raise click.ClickException(f"no connection with id {connection_id!r}")
    return row


def _check_connection_match(connection: Connection, owner_email: str, label: str, email: str) -> None:
    # Same non-interactive correctness check as delete-artifact, and the
    # same reasoning for not revealing which field was wrong — see
    # delete-artifact's comment.
    if (connection.label or "") != label or owner_email != email:
        raise click.ClickException(
            "label/email don't match this connection — not proceeding. If the caller doesn't "
            "have the correct label and owner email, something upstream is wrong; don't guess."
        )


async def _revoke_token(connection_id: str, label: str, email: str) -> None:
    async with session_scope() as session:
        connection, owner_email = await _lookup_connection(session, connection_id)
        _check_connection_match(connection, owner_email, label, email)
    # Shared helper: harmlessly no-ops the OAuth-token cleanup for a
    # personal-token connection, since it has none.
    await oauth_provider.revoke_connection(connection_id)


@main.command("revoke-token")
@click.option("--id", "connection_id", required=True, help="Connection id (see the dashboard's Connections page)")
@click.option("--label", required=True, help="Connection's exact label — pass \"\" if it has none")
@click.option("--email", required=True, help="Exact email of the connection's owner")
def revoke_token_cmd(connection_id: str, label: str, email: str) -> None:
    """Revoke any personal token or OAuth-authorized connection by id,
    killing its access immediately (idempotent if already revoked) while
    keeping its history, bypassing the per-owner scoping the dashboard
    enforces. --label and --email must exactly match the connection's
    actual label and owner, or nothing happens — a non-interactive
    correctness check, not a confirmation prompt, since this command is
    expected to run non-interactively (typically invoked by an agent).
    Never exposed as an MCP tool or a dashboard action — shell access to
    the deployed container is the trust boundary here, same as
    create-user/create-token/delete-artifact."""
    asyncio.run(_revoke_token(connection_id, label, email))
    click.echo("Revoked.")


async def _delete_token(connection_id: str, label: str, email: str) -> None:
    async with session_scope() as session:
        connection, owner_email = await _lookup_connection(session, connection_id)
        _check_connection_match(connection, owner_email, label, email)
        try:
            await tools.admin_delete_connection(session, connection_id)
        except tools.NotFoundError:
            raise click.ClickException("connection was deleted by something else just now") from None
        except tools.ConnectionNotRevokedError:
            raise click.ClickException("only revoked connections can be deleted — run revoke-token first") from None
        except tools.ConnectionHasHistoryError as exc:
            raise click.ClickException(str(exc)) from None


@main.command("delete-token")
@click.option("--id", "connection_id", required=True, help="Connection id (see the dashboard's Connections page)")
@click.option("--label", required=True, help="Connection's exact label — pass \"\" if it has none")
@click.option("--email", required=True, help="Exact email of the connection's owner")
def delete_token_cmd(connection_id: str, label: str, email: str) -> None:
    """Permanently delete a revoked personal token or OAuth-authorized
    connection by id — blocked if it ever published an artifact, left a
    comment, or shared something (revoked connections keep their history;
    delete the artifacts themselves first if that content should go too
    — see delete-artifact). --label and --email must exactly match, or
    nothing is deleted — a non-interactive correctness check, not a
    confirmation prompt, since this command is expected to run
    non-interactively (typically invoked by an agent). Never exposed as
    an MCP tool or a dashboard action — shell access to the deployed
    container is the trust boundary here, same as
    create-user/create-token/delete-artifact."""
    asyncio.run(_delete_token(connection_id, label, email))
    click.echo("Deleted.")


async def _link_oidc_identity(email: str, issuer: str, subject: str) -> None:
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            raise click.ClickException(f"no user with email {email!r}")

        existing = (
            await session.execute(
                select(OidcIdentity).where(OidcIdentity.issuer == issuer, OidcIdentity.subject == subject)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.user_id == user.id:
                raise click.ClickException("this identity is already linked to that user")
            raise click.ClickException("this issuer/subject is already linked to a different user")

        session.add(
            OidcIdentity(id=str(uuid.uuid4()), user_id=user.id, issuer=issuer, subject=subject, created_at=utcnow())
        )
        await session.commit()


@main.command("link-oidc-identity")
@click.option("--email", required=True, help="Email of the existing renderdesk user to link")
@click.option("--issuer", required=True, help="Exact 'issuer' value from the IdP's /.well-known/openid-configuration")
@click.option("--subject", required=True, help="The IdP's 'sub' claim for this person")
def link_oidc_identity_cmd(email: str, issuer: str, subject: str) -> None:
    """Link an external OIDC identity to an existing renderdesk user, so
    they can log in via SSO instead of (or alongside) a password. Needed
    because a first-time OIDC login is deliberately never auto-linked to
    an existing account by matching email alone, to close an
    account-takeover path — see dashboard.oidc_callback and
    DESIGN_NOTES.md. Get --issuer from the IdP's discovery document (must
    match exactly, including trailing slash) and --subject from however
    the IdP is configured to populate the sub claim (e.g. Authentik's
    provider-level 'Subject mode' setting). Never exposed as an MCP tool
    or a dashboard action — shell access to the deployed container is the
    trust boundary here, same as create-user/create-token."""
    asyncio.run(_link_oidc_identity(email, issuer, subject))
    click.echo(f"Linked {email} to issuer {issuer!r}, subject {subject!r}.")


async def _unlink_oidc_identity(issuer: str, subject: str, email: str) -> None:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(OidcIdentity, User.email)
                .join(User, User.id == OidcIdentity.user_id)
                .where(OidcIdentity.issuer == issuer, OidcIdentity.subject == subject)
            )
        ).first()
        if row is None:
            raise click.ClickException(f"no identity linked for issuer {issuer!r}, subject {subject!r}")
        identity, owner_email = row

        # A correctness check, not a confirmation prompt — same reasoning
        # as delete-artifact/revoke-token: this command is expected to run
        # non-interactively, and requiring the caller to already know the
        # linked owner's email catches a wrong/copy-pasted issuer or
        # subject before it unlinks someone else's identity.
        if owner_email != email:
            raise click.ClickException(
                "email doesn't match this identity's linked user — not unlinking. If the caller doesn't "
                "have the correct owner email, something upstream is wrong; don't guess."
            )

        await session.delete(identity)
        await session.commit()


@main.command("unlink-oidc-identity")
@click.option("--issuer", required=True, help="Exact 'issuer' value the identity was linked with")
@click.option("--subject", required=True, help="The IdP's 'sub' claim for the identity to unlink")
@click.option("--email", required=True, help="Exact email of the renderdesk user this identity is linked to")
def unlink_oidc_identity_cmd(issuer: str, subject: str, email: str) -> None:
    """Remove a previously linked OIDC identity — to clean up a mistaken
    link-oidc-identity call, or revoke SSO access for one IdP account
    without touching a user's other logins or their password. --email
    must exactly match the identity's currently linked user, or nothing
    is unlinked. Never exposed as an MCP tool or a dashboard action —
    shell access to the deployed container is the trust boundary here,
    same as create-user/create-token/link-oidc-identity."""
    asyncio.run(_unlink_oidc_identity(issuer, subject, email))
    click.echo("Unlinked.")


async def _set_auth_scheme(scheme: str) -> None:
    async with session_scope() as session:
        await _set_auth_scheme_persisted(session, scheme)


@main.command("set-auth-scheme")
@click.argument("scheme", type=click.Choice(AUTH_SCHEMES))
def set_auth_scheme_cmd(scheme: str) -> None:
    """Deliberately change the persisted auth scheme. Required whenever
    RENDERDESK_AUTH_SCHEME is being changed on purpose — the app refuses to
    boot if the env var doesn't match what's already persisted, specifically
    so a scheme change can't happen by just editing the environment
    variable. Run this first, then update RENDERDESK_AUTH_SCHEME to match
    before the next restart."""
    asyncio.run(_set_auth_scheme(scheme))
    click.echo(f"Auth scheme set to {scheme!r}. Set RENDERDESK_AUTH_SCHEME={scheme} before the next restart.")


if __name__ == "__main__":
    main()
