import asyncio
import time
from contextlib import AsyncExitStack, asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import OperationalError

from renderdesk.auth import MCPAuthMiddleware
from renderdesk.dashboard import router as dashboard_router
from renderdesk.mcp_server import mcp
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
        except OperationalError:
            if attempt == 4:
                raise
            time.sleep(2)


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
app.include_router(view_router)
app.include_router(dashboard_router)
app.mount("/mcp", MCPAuthMiddleware(_mcp_asgi_app))


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")
