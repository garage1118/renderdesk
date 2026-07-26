import asyncio
import uuid

import bcrypt
import click
from sqlalchemy import select

from renderdesk.db import session_scope
from renderdesk.models import User, utcnow
from renderdesk.tokens import create_personal_token


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
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
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


if __name__ == "__main__":
    main()
