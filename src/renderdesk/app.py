from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from renderdesk.auth import MCPAuthMiddleware
from renderdesk.db import init_db
from renderdesk.mcp_server import mcp
from renderdesk.view import router as view_router

_mcp_asgi_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncExitStack() as stack:
        # streamable_http_app()'s own lifespan starts the session manager;
        # Starlette's Mount does not forward lifespan events to sub-apps,
        # so it has to be entered explicitly here.
        await stack.enter_async_context(_mcp_asgi_app.router.lifespan_context(_mcp_asgi_app))
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(view_router)
app.mount("/mcp", MCPAuthMiddleware(_mcp_asgi_app))
