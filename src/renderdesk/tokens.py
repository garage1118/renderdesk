import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.auth import generate_token, hash_token
from renderdesk.config import settings
from renderdesk.models import Connection, utcnow


async def create_personal_token(session: AsyncSession, user_id: str, label: str | None) -> str:
    """Mints a personal MCP bearer token for user_id. Shared by the CLI
    (renderdesk create-token) and the dashboard's self-serve 'Connections'
    page — same absolute 90-day-by-default expiry either way."""
    token = generate_token()
    session.add(
        Connection(
            id=str(uuid.uuid4()),
            user_id=user_id,
            token_hash=hash_token(token),
            label=label,
            expires_at=utcnow() + timedelta(days=settings.token_expiry_days),
        )
    )
    await session.commit()
    return token
