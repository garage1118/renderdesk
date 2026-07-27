import secrets
from urllib.parse import urlparse

from fastapi import Form, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from renderdesk.config import settings

CSRF_COOKIE_NAME = "csrf_token"
CSRF_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days — same order of magnitude as a session

# Same rule as the session cookie in dashboard.py: only mark Secure when
# actually serving https, or a browser would refuse to send it back over
# plain http in local dev.
_COOKIE_SECURE = urlparse(settings.public_base_url).scheme == "https"


class CSRFCookieMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF protection for the dashboard's plain HTML
    forms — no JS/fetch involved anywhere, so the token has to travel as a
    normal cookie plus a hidden form field, not a custom header.

    Every request gets `request.state.csrf_token` set to the existing
    cookie's value, or a freshly generated one if there wasn't one yet
    (first visit); the fresh value is written back as a Set-Cookie if the
    response doesn't already carry the matching one. Templates embed this
    same value as a hidden field (see `csrf_token(request)` Jinja global in
    dashboard.py) on every state-changing form; `verify_csrf` below checks
    that the submitted field matches the cookie on the way back in. A
    cross-site attacker's forged form has no way to read our cookie value,
    so it can never produce a match.
    """

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(CSRF_COOKIE_NAME)
        is_new = token is None
        if is_new:
            token = secrets.token_urlsafe(32)
        request.state.csrf_token = token

        response = await call_next(request)

        if is_new:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                token,
                httponly=True,
                secure=_COOKIE_SECURE,
                samesite="lax",
                max_age=CSRF_COOKIE_MAX_AGE,
            )
        return response


async def verify_csrf(request: Request, csrf_token: str = Form(...)) -> None:
    """FastAPI dependency for every state-changing dashboard/oauth POST
    route. Compares the submitted form field against the cookie in
    constant time; a mismatch or missing cookie (e.g. a request that never
    went through a GET first) is rejected outright."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token or not secrets.compare_digest(cookie_token, csrf_token):
        raise HTTPException(status_code=403, detail="invalid or missing CSRF token")
