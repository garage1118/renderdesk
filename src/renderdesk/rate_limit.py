from collections import defaultdict
from datetime import datetime, timedelta

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from renderdesk.models import utcnow

# In-memory fixed-window limiter, keyed by (bucket, key) — same shape and
# same reasoning as session_auth._failed_logins: fine for a single-process
# deploy (one uvicorn worker, see Dockerfile), no need to persist attempt
# history across restarts or pull in a rate-limiting dependency.
_attempts: dict[tuple[str, str], list[datetime]] = defaultdict(list)


def is_rate_limited(bucket: str, key: str, max_attempts: int, window: timedelta) -> bool:
    cutoff = utcnow() - window
    entry_key = (bucket, key)
    attempts = [t for t in _attempts.get(entry_key, []) if t > cutoff]
    if attempts:
        _attempts[entry_key] = attempts
    else:
        _attempts.pop(entry_key, None)
    return len(attempts) >= max_attempts


def record_attempt(bucket: str, key: str) -> None:
    _attempts[(bucket, key)].append(utcnow())


# (bucket, max_attempts, window, count_on_success) per rate-limited
# endpoint. Module-level rather than a class attribute on the middleware so
# _MAX_WINDOW below can be derived from it — a new rule with a longer
# window then widens the sweep automatically instead of silently outliving
# it. count_on_success is False only for login_ip: a successful login
# shouldn't spend down the same budget that's meant to slow down wrong-
# password guesses (see the dispatch logic below).
_RULES: dict[tuple[str, str], tuple[str, int, timedelta, bool]] = {
    ("POST", "/register"): ("register", 5, timedelta(hours=1), True),
    ("GET", "/authorize"): ("oauth_coarse", 60, timedelta(minutes=5), True),
    ("POST", "/authorize"): ("oauth_coarse", 60, timedelta(minutes=5), True),
    ("POST", "/token"): ("oauth_coarse", 60, timedelta(minutes=5), True),
    ("POST", "/revoke"): ("oauth_coarse", 60, timedelta(minutes=5), True),
    ("POST", "/dashboard/login"): ("login_ip", 20, timedelta(minutes=15), False),
}
_MAX_WINDOW = max(window for _, _, window, _count_on_success in _RULES.values())


def sweep_stale_attempts() -> int:
    """Drops entries whose newest attempt already fell out of the longest
    configured window, so none of them can still be limiting anything.

    is_rate_limited prunes timestamps, but only for a key someone asks
    about again — an entry touched once and never revisited is never
    reconsidered. Since the keys here are client IPs chosen by
    unauthenticated callers, that's unbounded growth from remote input:
    rotating source addresses adds a dict entry per address for the
    lifetime of the process. Small per entry, never reclaimed. Called from
    app.py's sweep loop."""
    cutoff = utcnow() - _MAX_WINDOW
    stale = [key for key, times in _attempts.items() if not times or max(times) <= cutoff]
    for key in stale:
        del _attempts[key]
    return len(stale)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse, per-IP flood protection for the endpoints that had none at
    all: dynamic client registration (unauthenticated, unbounded, and each
    call permanently adds a row on a disk-constrained SQLite file) and the
    rest of the OAuth token surface, plus an IP dimension on login on top
    of session_auth's existing per-email lockout (which alone lets anyone
    lock out a known user indefinitely with 5 requests).

    request.client.host is trustworthy only if RENDERDESK_TRUSTED_PROXY_IPS
    is actually set in the deploy environment (docker-entrypoint.sh passes
    it to uvicorn's --forwarded-allow-ips) — that variable is never baked
    into the image, so a deployment behind a reverse proxy that forgets to
    set it gets every request attributed to the proxy's own address, and
    every limit here collapses into one shared bucket for every real
    client (CLAUDE-SECURITY-RESULTS.md F8; app.py logs a startup warning
    for this case, since it can't be detected or fixed from inside the
    app).
    """

    async def dispatch(self, request: Request, call_next):
        rule = _RULES.get((request.method, request.url.path))
        if rule is None:
            return await call_next(request)

        bucket, max_attempts, window, count_on_success = rule
        client_ip = request.client.host if request.client else "unknown"
        if is_rate_limited(bucket, client_ip, max_attempts, window):
            return PlainTextResponse("Too many requests", status_code=429)

        if count_on_success:
            record_attempt(bucket, client_ip)
            return await call_next(request)

        # login_ip: only a failed attempt should spend down the budget —
        # a legitimate user logging in repeatedly (or re-authenticating
        # after a session expires) shouldn't get themselves rate-limited
        # by their own successful logins. login_submit returns 303 only on
        # success, so anything else (401 wrong credentials, 429 already
        # locked by the per-email lockout, etc.) counts as an attempt.
        response = await call_next(request)
        if response.status_code != 303:
            record_attempt(bucket, client_ip)
        return response
