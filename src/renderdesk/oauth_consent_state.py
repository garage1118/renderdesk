import hashlib
import hmac
import secrets
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from renderdesk.config import settings

OAUTH_FLOW_COOKIE_NAME = "renderdesk_oauth_flow"
OAUTH_FLOW_MAX_AGE = 600  # matches oauth_provider.AUTHORIZATION_CODE_TTL

_COOKIE_SECURE = urlparse(settings.public_base_url).scheme == "https"

# Same reasoning as oidc_state._SIGNING_KEY: transient, in-process-only,
# never needs to survive a restart since a mid-flight authorization just
# gets retried from the client application.
_SIGNING_KEY = secrets.token_bytes(32)


def _sign(request_id: str) -> str:
    return hmac.new(_SIGNING_KEY, request_id.encode(), hashlib.sha256).hexdigest()


def make_cookie_value(request_id: str) -> str:
    return f"{request_id}.{_sign(request_id)}"


def read_cookie_value(raw: str) -> str | None:
    """Returns the request_id the cookie was stamped with, or None if the
    cookie is missing/malformed/tampered."""
    try:
        request_id, sig = raw.rsplit(".", 1)
    except ValueError:
        return None
    if not secrets.compare_digest(_sign(request_id), sig):
        return None
    return request_id


class OAuthConsentBindingMiddleware(BaseHTTPMiddleware):
    """Stamps a signed cookie on the browser that receives the redirect from
    the SDK's /authorize route to /oauth/consent?request_id=..., binding the
    pending authorization request to that specific browser.

    Without this, a request_id minted for one browser (the one that hit
    /authorize) is just as usable from any other browser it's handed to —
    the classic consent-phishing shape: an attacker starts the flow, sends
    the resulting consent URL to a victim, and the victim's approval mints a
    token for the attacker's client against the victim's account. Binding
    the request_id to the browser that actually received the redirect closes
    that, since an attacker can copy the URL but not the cookie.

    Implemented as response-inspection middleware (rather than a change
    inside RenderdeskOAuthProvider.authorize) because that method only
    returns a redirect target string — the mcp SDK's own route handler owns
    the Response object and isn't ours to modify.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        location = response.headers.get("location", "")
        # Must actually be the SDK's /authorize route producing this
        # redirect — not just any response that happens to redirect to
        # /oauth/consent?... . Without this check, login_submit's 303 to a
        # `next=/oauth/consent?request_id=R` value (reachable via
        # safe_next_path, which had no reason to know this path was
        # special) re-stamped the binding cookie for an R the browser never
        # actually obtained from /authorize, defeating the binding this
        # middleware exists to provide.
        if (
            response.status_code in (302, 303, 307, 308)
            and location.startswith("/oauth/consent?")
            and request.url.path == "/authorize"
        ):
            request_id = parse_request_id_from_location(location)
            if request_id is not None:
                response.set_cookie(
                    OAUTH_FLOW_COOKIE_NAME,
                    make_cookie_value(request_id),
                    httponly=True,
                    secure=_COOKIE_SECURE,
                    samesite="lax",
                    max_age=OAUTH_FLOW_MAX_AGE,
                )
        return response


def parse_request_id_from_location(location: str) -> str | None:
    query = urlparse(location).query
    for part in query.split("&"):
        key, _, value = part.partition("=")
        if key == "request_id" and value:
            return value
    return None
