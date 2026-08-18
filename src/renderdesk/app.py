import asyncio
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from renderdesk.auth import MCPAuthMiddleware
from renderdesk.auth_scheme import ensure_auth_scheme
from renderdesk.body_limits import MaxBodySizeMiddleware
from renderdesk.config import settings
from renderdesk.csrf import CSRFCookieMiddleware
from renderdesk.dashboard import router as dashboard_router
from renderdesk.db import engine, session_scope
from renderdesk.mcp_server import mcp
from renderdesk.oauth_consent_state import OAuthConsentBindingMiddleware
from renderdesk.oauth_provider import oauth_provider, sweep_expired_oauth_rows
from renderdesk.oidc_state import sweep_stale_used_states
from renderdesk.rate_limit import RateLimitMiddleware, sweep_stale_attempts
from renderdesk.security_headers import SecurityHeadersMiddleware
from renderdesk.session_auth import sweep_expired_sessions, sweep_stale_failed_logins
from renderdesk.static_files import CORSStaticFiles
from renderdesk.view import router as view_router

_SWEEP_INTERVAL = timedelta(hours=1)

_logger = logging.getLogger("renderdesk")

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


async def _sweep_loop() -> None:
    # Nothing else ever cleans any of this up, because each store is only
    # ever pruned as a side effect of someone touching the exact row/key
    # again — which never happens for the abandoned ones, precisely the
    # ones worth reclaiming:
    #  - OAuth rows: pending or issued authorization codes, expired refresh
    #    tokens, clients that never completed a token exchange.
    #  - Session rows: expired sessions nobody came back to present.
    #  - The in-memory rate-limit/login-lockout counters, whose keys (client
    #    IP, submitted email) are chosen by unauthenticated callers, so
    #    they'd otherwise grow without bound for the process's lifetime.
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL.total_seconds())
        # One failing sweep must never end this loop permanently — with no
        # try/except here, an exception from any call below (e.g. F14's
        # OAuthClient foreign-key violation) used to propagate out of the
        # task and kill it for the rest of the process's life, silently
        # leaving nothing to prune any of this ever again.
        try:
            await sweep_expired_oauth_rows()
            await sweep_expired_sessions()
            sweep_stale_attempts()
            sweep_stale_failed_logins()
            sweep_stale_used_states()
        except Exception:
            _logger.exception("sweep loop iteration failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # If unset, X-Forwarded-For is silently ignored (uvicorn only trusts
    # 127.0.0.1 by default) and every request behind a real reverse proxy
    # appears to share the proxy's address — collapsing the login lockout
    # and OAuth rate limits (rate_limit.py) into one bucket for every real
    # client. This can't be detected from inside the app (the peer address
    # alone doesn't say "this is a proxy"), so it's a loud startup log
    # rather than a refusal to start —
    # a direct-to-internet deployment with no reverse proxy legitimately
    # has nothing to set here.
    if not settings.trusted_proxy_ips:
        _logger.warning(
            "RENDERDESK_TRUSTED_PROXY_IPS is unset. If this instance is behind a "
            "reverse proxy, X-Forwarded-For will be ignored and every request will "
            "appear to come from the proxy's address, collapsing all IP-based rate "
            "limits into one shared bucket. Set it to the proxy's IP or CIDR."
        )
    # Runs synchronously (Alembic has no async API) but only once at startup,
    # so it's offloaded to a thread rather than blocking the event loop.
    await asyncio.to_thread(_run_migrations)
    # Must run after migrations (needs the app_settings table) and before
    # anything starts serving traffic — an auth scheme mismatch is a startup
    # failure, not a runtime one. See auth_scheme.py for why this can't just
    # be "whatever the env var currently says."
    async with session_scope() as session:
        await ensure_auth_scheme(session, settings.auth_scheme)
    sweep_task = asyncio.create_task(_sweep_loop())
    try:
        async with AsyncExitStack() as stack:
            # streamable_http_app()'s own lifespan starts the session manager;
            # Starlette's Mount does not forward lifespan events to sub-apps,
            # so it has to be entered explicitly here.
            await stack.enter_async_context(_mcp_asgi_app.router.lifespan_context(_mcp_asgi_app))
            yield
    finally:
        sweep_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CSRFCookieMiddleware)
app.add_middleware(OAuthConsentBindingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
# Added last so it ends up outermost (see MaxBodySizeMiddleware's
# docstring) — rejects an oversized body before any other middleware, or
# the MCP SDK/OAuth registration handler underneath, ever buffers it.
# /mcp's ceiling is a multiple of max_bytes_per_artifact rather than equal
# to it: the documented upload_large_artifact workflow streams content
# straight at /mcp inside a JSON-RPC envelope, which adds overhead beyond
# the raw artifact bytes. /register's is independent of artifact size —
# oauth_provider._MAX_CLIENT_METADATA_BYTES bounds the persisted
# metadata; this just has to comfortably clear that plus JSON framing so
# real registrations are never rejected here first.
app.add_middleware(
    MaxBodySizeMiddleware,
    limits=[
        ("/mcp", 4 * settings.max_bytes_per_artifact),
        ("/register", 32_768),
    ],
)
app.include_router(view_router)
app.include_router(dashboard_router)
app.mount("/mcp", MCPAuthMiddleware(_mcp_asgi_app))
app.mount("/static", CORSStaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Mounted on the top-level app (not nested under /mcp) so paths match
# issuer_url cleanly, and so this doesn't stack a second bearer-auth layer on
# top of MCPAuthMiddleware, which already fully owns the /mcp mount.
app.router.routes.extend(
    create_auth_routes(
        provider=oauth_provider,
        issuer_url=AnyHttpUrl(settings.public_base_url),
        # "mcp" is the only scope that means anything today — the whole
        # tool surface is one undifferentiated capability level, nothing
        # enforces finer-grained access (see DESIGN_NOTES.md: real scope
        # enforcement is future work, not something to half-build here).
        # valid_scopes still matters now: without it a registrant can
        # claim any scope string it likes, and the consent screen used to
        # render that string as if it meant something — the template no
        # longer shows it, but this keeps a registered client's own
        # metadata honest too.
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
        ),
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
