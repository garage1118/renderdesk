import asyncio
import uuid
from datetime import timedelta

import click

from renderdesk.auth import generate_token, hash_token
from renderdesk.config import settings
from renderdesk.db import init_db, session_scope
from renderdesk.models import Connection, utcnow


async def _create_token(label: str | None) -> str:
    await init_db()
    token = generate_token()
    connection = Connection(
        id=str(uuid.uuid4()),
        token_hash=hash_token(token),
        label=label,
        expires_at=utcnow() + timedelta(days=settings.token_expiry_days),
    )
    async with session_scope() as session:
        session.add(connection)
        await session.commit()
    return token


@click.command()
@click.option("--label", default=None, help="Optional label to identify this connection (e.g. 'claude-code')")
def main(label: str | None) -> None:
    """Issue a new MCP bearer token. This is the only way to create one in Stage 1 —
    there is no token-issuing MCP tool or web UI."""
    token = asyncio.run(_create_token(label))
    click.echo("Token (save this now, it will not be shown again):")
    click.echo(token)


if __name__ == "__main__":
    main()
