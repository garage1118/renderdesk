import click
import pytest
from click.testing import CliRunner
from sqlalchemy import select

from renderdesk.cli import _link_oidc_identity, _unlink_oidc_identity, create_user
from renderdesk.db import session_scope
from renderdesk.models import OidcIdentity

from .conftest import make_user


def test_create_user_rejects_short_password():
    runner = CliRunner()
    result = runner.invoke(create_user, ["--email", "short@example.com"], input="short\nshort\n")
    assert result.exit_code != 0
    assert "at least 8 characters" in result.output


def test_create_user_rejects_malformed_email():
    runner = CliRunner()
    result = runner.invoke(create_user, ["--email", "not-an-email"], input="longenoughpw\nlongenoughpw\n")
    assert result.exit_code != 0
    assert "email" in result.output.lower()


async def test_link_oidc_identity_creates_identity_row():
    await make_user(email="sso@example.com")
    await _link_oidc_identity("sso@example.com", "https://authentik.example.com/application/o/renderdesk/", "wavefront")

    async with session_scope() as session:
        identity = (
            await session.execute(
                select(OidcIdentity).where(
                    OidcIdentity.issuer == "https://authentik.example.com/application/o/renderdesk/",
                    OidcIdentity.subject == "wavefront",
                )
            )
        ).scalar_one_or_none()
    assert identity is not None


async def test_link_oidc_identity_rejects_unknown_email():
    with pytest.raises(click.ClickException, match="no user with email"):
        await _link_oidc_identity("nobody@example.com", "https://idp.example.com/", "x")


async def test_link_oidc_identity_rejects_duplicate_issuer_subject():
    await make_user(email="one@example.com")
    await make_user(email="two@example.com")
    await _link_oidc_identity("one@example.com", "https://idp.example.com/", "same-subject")

    with pytest.raises(click.ClickException, match="already linked to a different user"):
        await _link_oidc_identity("two@example.com", "https://idp.example.com/", "same-subject")


async def test_unlink_oidc_identity_removes_identity_row():
    await make_user(email="sso@example.com")
    await _link_oidc_identity("sso@example.com", "https://idp.example.com/", "akadmin")

    await _unlink_oidc_identity("https://idp.example.com/", "akadmin", "sso@example.com")

    async with session_scope() as session:
        identity = (
            await session.execute(
                select(OidcIdentity).where(
                    OidcIdentity.issuer == "https://idp.example.com/", OidcIdentity.subject == "akadmin"
                )
            )
        ).scalar_one_or_none()
    assert identity is None


async def test_unlink_oidc_identity_rejects_unknown_identity():
    with pytest.raises(click.ClickException, match="no identity linked"):
        await _unlink_oidc_identity("https://idp.example.com/", "nobody", "sso@example.com")


async def test_unlink_oidc_identity_rejects_email_mismatch():
    await make_user(email="sso@example.com")
    await _link_oidc_identity("sso@example.com", "https://idp.example.com/", "akadmin")

    with pytest.raises(click.ClickException, match="email doesn't match"):
        await _unlink_oidc_identity("https://idp.example.com/", "akadmin", "wrong@example.com")

    async with session_scope() as session:
        identity = (
            await session.execute(
                select(OidcIdentity).where(
                    OidcIdentity.issuer == "https://idp.example.com/", OidcIdentity.subject == "akadmin"
                )
            )
        ).scalar_one_or_none()
    assert identity is not None
