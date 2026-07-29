import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlparse

from renderdesk.config import settings

OIDC_STATE_COOKIE_NAME = "renderdesk_oidc_state"
OIDC_STATE_MAX_AGE = 600  # 10 min: generous for a human login, short enough to bound a stale cookie

_COOKIE_SECURE = urlparse(settings.public_base_url).scheme == "https"

# Signs the transient state/nonce/PKCE-verifier cookie carried between the
# redirect-to-IdP and the callback. Deliberately in-process-only, not
# persisted anywhere — a login mid-flight during a restart just gets
# retried, so nothing here needs to survive one (same reasoning
# session_auth.py already uses for its in-memory login-lockout tracker).
_SIGNING_KEY = secrets.token_bytes(32)


def _sign(payload: bytes) -> str:
    return hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def make_cookie_value(state: str, nonce: str, code_verifier: str, next_path: str) -> str:
    payload = json.dumps({"s": state, "n": nonce, "v": code_verifier, "next": next_path}).encode()
    # Padding-stripped, like secrets.token_urlsafe — a "=" in the value
    # falls outside the unquoted cookie-value charset, so http.cookies
    # would wrap it in double quotes, and httpx's cookie jar (unlike a
    # real browser) doesn't strip those back off on the way in.
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def read_cookie_value(raw: str) -> dict | None:
    """Returns the decoded {"s", "n", "v", "next"} dict, or None if the
    cookie is missing/malformed/tampered — callers must never crash on a
    forged or expired-and-evicted state cookie."""
    try:
        body, sig = raw.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        return None
    if not secrets.compare_digest(_sign(payload), sig):
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None
