import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from renderdesk.auth import MCPAuthMiddleware
from renderdesk.config import settings
from renderdesk.csrf import CSRFCookieMiddleware
from renderdesk.dashboard import router as dashboard_router
from renderdesk.db import engine
from renderdesk.mcp_server import mcp
from renderdesk.oauth_provider import oauth_provider
from renderdesk.view import router as view_router

_mcp_asgi_app = mcp.streamable_http_app()


def _run_migrations() -> None:
    # Relative to CWD, matching config.py's database_path convention — the
    # app is always run from the repo root locally and from WORKDIR /app in
    # the container, where alembic.ini/migrations/ are also placed.
    #
    # Retries on OperationalError ("database is locked"): a deploy that swaps
    # the old container for the new one can leave a brief window where both
    # have the SQLite file open, which is enough to make a multi-statement
    # migration fail. Seen once in practice — a plain retry a few seconds
    # later succeeded cleanly once the old container's handle was gone.
    for attempt in range(5):
        try:
            command.upgrade(Config("alembic.ini"), "head")
            return
        except OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == 4:
                raise
            time.sleep(2**attempt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs synchronously (Alembic has no async API) but only once at startup,
    # so it's offloaded to a thread rather than blocking the event loop.
    await asyncio.to_thread(_run_migrations)
    async with AsyncExitStack() as stack:
        # streamable_http_app()'s own lifespan starts the session manager;
        # Starlette's Mount does not forward lifespan events to sub-apps,
        # so it has to be entered explicitly here.
        await stack.enter_async_context(_mcp_asgi_app.router.lifespan_context(_mcp_asgi_app))
        yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CSRFCookieMiddleware)
app.include_router(view_router)
app.include_router(dashboard_router)
app.mount("/mcp", MCPAuthMiddleware(_mcp_asgi_app))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Mounted on the top-level app (not nested under /mcp) so paths match
# issuer_url cleanly, and so this doesn't stack a second bearer-auth layer on
# top of MCPAuthMiddleware, which already fully owns the /mcp mount.
app.router.routes.extend(
    create_auth_routes(
        provider=oauth_provider,
        issuer_url=AnyHttpUrl(settings.public_base_url),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
)
app.router.routes.extend(
    create_protected_resource_routes(
        resource_url=AnyHttpUrl(f"{settings.public_base_url}/mcp"),
        authorization_servers=[AnyHttpUrl(settings.public_base_url)],
    )
)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    # Confirms the app can actually reach its database, not just that the
    # process is alive — a locked/corrupted SQLite file should fail this.
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
