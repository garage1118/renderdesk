import time

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebKey, jwt
from authlib.jose.errors import JoseError as AuthlibJoseError
from authlib.oidc.core import CodeIDToken
from joserfc.errors import JoseError as JoserfcJoseError

from renderdesk.config import settings

_REDIRECT_PATH = "/dashboard/auth/oidc/callback"
_JWKS_CACHE_TTL = 3600  # a same-day key rotation just forces one retry, not a hard failure

# authlib's jwt.decode() itself raises authlib.jose.errors.JoseError on
# structural/signature failures, but CodeIDToken.validate() (nonce/iss/aud/
# exp claims checks) is implemented on top of joserfc internally and raises
# joserfc.errors.JoseError instead — a genuinely separate exception
# hierarchy, verified against the installed authlib version. Both need
# catching or a bad nonce/aud/exp would surface as an unhandled 500.
_TokenValidationError = (AuthlibJoseError, JoserfcJoseError)

_discovery_cache: dict | None = None
_jwks_cache = None
_jwks_cache_at = 0.0


class OidcError(Exception):
    """Any failure in the IdP round trip — always surfaced as a clean login-page error, never a 500."""


def redirect_uri() -> str:
    return f"{settings.public_base_url}{_REDIRECT_PATH}"


async def _discovery() -> dict:
    global _discovery_cache
    if _discovery_cache is None:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.oidc_issuer_url.rstrip('/')}/.well-known/openid-configuration")
            resp.raise_for_status()
            _discovery_cache = resp.json()
    return _discovery_cache


async def _jwks(force_refresh: bool = False):
    global _jwks_cache, _jwks_cache_at
    if force_refresh or _jwks_cache is None or time.monotonic() - _jwks_cache_at > _JWKS_CACHE_TTL:
        discovery = await _discovery()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(discovery["jwks_uri"])
            resp.raise_for_status()
            _jwks_cache = JsonWebKey.import_key_set(resp.json())
            _jwks_cache_at = time.monotonic()
    return _jwks_cache


async def build_authorization_url(state: str, nonce: str, code_verifier: str) -> str:
    try:
        discovery = await _discovery()
    except httpx.HTTPError as exc:
        raise OidcError(f"could not reach identity provider: {exc}") from exc
    async with AsyncOAuth2Client(
        client_id=settings.oidc_client_id,
        redirect_uri=redirect_uri(),
        scope="openid email profile",
        code_challenge_method="S256",
    ) as client:
        url, _ = client.create_authorization_url(
            discovery["authorization_endpoint"], state=state, nonce=nonce, code_verifier=code_verifier
        )
    return url


async def exchange_code_and_validate(code: str, code_verifier: str, nonce: str) -> dict:
    """Exchanges an authorization code for tokens and returns validated ID
    token claims (iss/sub/email at minimum), or raises OidcError."""
    try:
        discovery = await _discovery()
    except httpx.HTTPError as exc:
        raise OidcError(f"could not reach identity provider: {exc}") from exc

    async with AsyncOAuth2Client(
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=redirect_uri(),
    ) as client:
        try:
            token = await client.fetch_token(
                discovery["token_endpoint"], code=code, code_verifier=code_verifier, grant_type="authorization_code"
            )
        except Exception as exc:
            raise OidcError("token exchange with the identity provider failed") from exc

    id_token = token.get("id_token")
    if not id_token:
        raise OidcError("identity provider did not return an id_token")

    claims = None
    last_exc: Exception | None = None
    for attempt in range(2):  # one retry with a forced JWKS refresh, covers same-day key rotation
        try:
            jwks = await _jwks(force_refresh=attempt == 1)
            claims = jwt.decode(
                id_token,
                jwks,
                claims_cls=CodeIDToken,
                claims_options={
                    "iss": {"essential": True, "value": discovery["issuer"]},
                    "aud": {"essential": True, "value": settings.oidc_client_id},
                },
                claims_params={"nonce": nonce},
            )
            claims.validate(leeway=60)
            last_exc = None
            break
        except _TokenValidationError as exc:
            last_exc = exc
    if last_exc is not None:
        raise OidcError(f"ID token validation failed: {last_exc}") from last_exc

    # "email" isn't reliably populated by every IdP for every account type —
    # notably Entra guest (B2B-invited) users, where it's frequently absent
    # even with the email scope granted. preferred_username/upn are left
    # here as-is (not merged into "email") for the caller to use as a
    # display-only fallback when provisioning a brand-new account —
    # neither is spec-guaranteed unique or stable, so neither is safe to
    # use as an account-matching key (see dashboard.oidc_callback).
    claims_dict = dict(claims)
    if not (claims_dict.get("email") or claims_dict.get("preferred_username") or claims_dict.get("upn")):
        raise OidcError("identity provider did not return an email, preferred_username, or upn claim")
    return claims_dict
