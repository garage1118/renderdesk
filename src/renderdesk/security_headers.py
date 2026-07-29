from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# Applied to every response that doesn't already set its own value for a
# given header — view.py sets a tailored Content-Security-Policy per
# artifact format/embedding mode, and this must never override that.
_DEFAULT_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "frame-ancestors 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security headers for the dashboard/login/consent/OAuth
    surface, none of which set any of these today. Clickjacking is largely
    neutralized in practice by the session cookie's SameSite=Lax, but that
    shouldn't be the only thing standing between a framed dashboard and a
    logged-in user."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in _DEFAULT_HEADERS.items():
            if name.lower() not in response.headers:
                response.headers[name] = value
        return response
