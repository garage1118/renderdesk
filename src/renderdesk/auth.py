import hashlib
import secrets
from contextvars import ContextVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from renderdesk.db import session_scope
from renderdesk.models import Connection, utcnow

# Set by MCPAuthMiddleware for the duration of a single /mcp request;
# read by tools.py to scope every operation to the calling connection.
current_connection_id: ContextVar[str | None] = ContextVar("current_connection_id", default=None)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def resolve_connection(session: AsyncSession, token: str) -> Connection | None:
    token_hash = hash_token(token)
    result = await session.execute(select(Connection).where(Connection.token_hash == token_hash))
    connection = result.scalar_one_or_none()
    if connection is None:
        return None
    if connection.revoked_at is not None:
        return None
    if connection.expires_at <= utcnow():
        return None
    return connection


def get_current_connection_id() -> str:
    connection_id = current_connection_id.get()
    if connection_id is None:
        raise RuntimeError("No connection_id set on this request context")
    return connection_id


class MCPAuthMiddleware:
    """Gates the /mcp mount on a bearer token, distinct from any other auth path
    in the app — the boundary that stops an MCP token from ever reaching
    anything but the four MCP tools."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        auth_header = headers.get("authorization", "")
        token = auth_header[len("Bearer ") :].strip() if auth_header.startswith("Bearer ") else None

        connection = None
        if token:
            async with session_scope() as session:
                connection = await resolve_connection(session, token)

        if connection is None:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        reset_token = current_connection_id.set(connection.id)
        try:
            await self.app(scope, receive, send)
        finally:
            current_connection_id.reset(reset_token)
