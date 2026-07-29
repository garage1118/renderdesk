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


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse, per-IP flood protection for the endpoints that had none at
    all: dynamic client registration (unauthenticated, unbounded, and each
    call permanently adds a row on a disk-constrained SQLite file) and the
    rest of the OAuth token surface, plus an IP dimension on login on top
    of session_auth's existing per-email lockout (which alone lets anyone
    lock out a known user indefinitely with 5 requests).

    request.client.host is trustworthy here: docker-entrypoint.sh passes
    RENDERDESK_TRUSTED_PROXY_IPS to uvicorn's --forwarded-allow-ips, so
    Starlette only honors X-Forwarded-For from a configured reverse proxy.
    """

    _RULES: dict[tuple[str, str], tuple[str, int, timedelta]] = {
        ("POST", "/register"): ("register", 5, timedelta(hours=1)),
        ("GET", "/authorize"): ("oauth_coarse", 60, timedelta(minutes=5)),
        ("POST", "/authorize"): ("oauth_coarse", 60, timedelta(minutes=5)),
        ("POST", "/token"): ("oauth_coarse", 60, timedelta(minutes=5)),
        ("POST", "/revoke"): ("oauth_coarse", 60, timedelta(minutes=5)),
        ("POST", "/dashboard/login"): ("login_ip", 20, timedelta(minutes=15)),
    }

    async def dispatch(self, request: Request, call_next):
        rule = self._RULES.get((request.method, request.url.path))
        if rule is not None:
            bucket, max_attempts, window = rule
            client_ip = request.client.host if request.client else "unknown"
            if is_rate_limited(bucket, client_ip, max_attempts, window):
                return PlainTextResponse("Too many requests", status_code=429)
            record_attempt(bucket, client_ip)
        return await call_next(request)
