from typing import Literal
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from renderdesk import tools
from renderdesk.auth import get_current_connection_id
from renderdesk.config import settings
from renderdesk.db import session_scope

# FastMCP auto-enables DNS-rebinding protection scoped to localhost because
# it assumes it's bound to 127.0.0.1. We're reverse-proxied under a real
# public host, so the Host header on every real request would otherwise be
# rejected with 421. Re-scope the allow-list to our actual public host
# instead of turning the protection off.
_public_url = urlparse(settings.public_base_url)
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[_public_url.netloc, f"{_public_url.hostname}:*"],
    allowed_origins=[f"{_public_url.scheme}://{_public_url.netloc}"],
)

mcp = FastMCP("renderdesk", streamable_http_path="/", transport_security=_transport_security)


@mcp.tool()
async def publish_artifact(content: str, format: Literal["html", "markdown"], title: str | None = None) -> dict:
    """Publish a new self-contained HTML or Markdown artifact and get back a shareable URL."""
    connection_id = get_current_connection_id()
    async with session_scope() as session:
        return await tools.publish_artifact(session, connection_id, content, format, title)


@mcp.tool()
async def update_artifact(
    artifact_id: str,
    content: str,
    base_version: int,
    format: Literal["html", "markdown"] | None = None,
    title: str | None = None,
) -> dict:
    """Update an artifact you previously published. base_version must match the artifact's
    current version (from publish_artifact/get_artifact/list_artifacts) or the update is
    rejected with a version_conflict error naming the current version to retry with."""
    connection_id = get_current_connection_id()
    async with session_scope() as session:
        return await tools.update_artifact(
            session, connection_id, artifact_id, content, base_version, format, title
        )


@mcp.tool()
async def get_artifact(artifact_id: str, include_content: bool = False) -> dict:
    """Get metadata (and optionally content) for an artifact you own."""
    connection_id = get_current_connection_id()
    async with session_scope() as session:
        return await tools.get_artifact(session, connection_id, artifact_id, include_content)


@mcp.tool()
async def list_artifacts(limit: int = 50) -> list[dict]:
    """List artifacts you've published, most recently updated first."""
    connection_id = get_current_connection_id()
    async with session_scope() as session:
        return await tools.list_artifacts(session, connection_id, limit)
