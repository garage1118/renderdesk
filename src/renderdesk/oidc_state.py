import base64
import hashlib
import hmac
import json
import secrets
import time
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

# One state cookie is meant to fund exactly one callback attempt.
# Server-side tracking is needed for that, not just cookie deletion — the
# cookie is a bare signed value with no persisted counterpart, so an
# attacker who has captured it (or minted their own via one GET
# /dashboard/auth/oidc/login) can otherwise replay the same cookie with a
# different `code` on every request. oidc_callback marks a state used
# before it does anything else with the request, regardless of whether the
# code exchange that follows succeeds — that's what stops a single
# self-funded state from driving unlimited outbound token-exchange
# attempts against the IdP (CLAUDE-SECURITY-RESULTS.md F16). In-memory and
# swept the same way as session_auth's/rate_limit's other unauthenticated-
# key trackers — fine for this app's single-process deployment.
_used_states: dict[str, float] = {}


def mark_state_used(state: str) -> None:
    _used_states[state] = time.time()


def is_state_used(state: str) -> bool:
    return state in _used_states


def sweep_stale_used_states() -> int:
    cutoff = time.time() - OIDC_STATE_MAX_AGE
    stale = [state for state, used_at in _used_states.items() if used_at <= cutoff]
    for state in stale:
        del _used_states[state]
    return len(stale)


def _sign(payload: bytes) -> str:
    return hmac.new(_SIGNING_KEY, payload, hashlib.sha256).hexdigest()


def make_cookie_value(state: str, nonce: str, code_verifier: str, next_path: str) -> str:
    payload = json.dumps({"s": state, "n": nonce, "v": code_verifier, "next": next_path, "iat": time.time()}).encode()
    # Padding-stripped, like secrets.token_urlsafe — a "=" in the value
    # falls outside the unquoted cookie-value charset, so http.cookies
    # would wrap it in double quotes, and httpx's cookie jar (unlike a
    # real browser) doesn't strip those back off on the way in.
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def read_cookie_value(raw: str) -> dict | None:
    """Returns the decoded {"s", "n", "v", "next", "iat"} dict, or None if
    the cookie is missing/malformed/tampered/expired — callers must never
    crash on a forged or expired-and-evicted state cookie.

    The cookie's own Max-Age (OIDC_STATE_MAX_AGE, set where the cookie is
    issued) only bounds a real browser — it's client-enforced and a
    scripted caller replaying the raw cookie value is free to ignore it.
    "iat" inside the *signed* payload is what actually bounds the cookie's
    lifetime server-side, closing that gap (CLAUDE-SECURITY-RESULTS.md
    F16)."""
    try:
        body, sig = raw.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except ValueError:
        return None
    if not secrets.compare_digest(_sign(payload), sig):
        return None
    try:
        flow = json.loads(payload)
    except ValueError:
        return None
    iat = flow.get("iat")
    if not isinstance(iat, int | float) or time.time() - iat > OIDC_STATE_MAX_AGE:
        return None
    return flow
