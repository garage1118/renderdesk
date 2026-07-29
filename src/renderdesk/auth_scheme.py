from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from renderdesk.models import AppSetting

# "password" is the only scheme with a real implementation today. "oidc"
# (generic — issuer/client config, works against Entra ID, Google
# Workspace, Okta, or a self-hosted IdP like Authentik/Keycloak/Authelia
# without renderdesk needing per-provider code) and "saml" are recognized
# as valid *configuration* values ahead of their actual implementation, so
# the persisted-scheme machinery doesn't need to change shape when they
# land — see DESIGN_NOTES.md's Roadmap.
AUTH_SCHEMES = ("password", "oidc", "saml")

_SETTING_KEY = "auth_scheme"

_active_scheme: str | None = None


class AuthSchemeMismatchError(Exception):
    pass


def get_active_auth_scheme() -> str:
    """The auth scheme this running process was booted with — resolved once
    by ensure_auth_scheme() at startup, fixed for the process's lifetime."""
    if _active_scheme is None:
        raise RuntimeError("auth scheme not yet resolved — ensure_auth_scheme() must run at startup")
    return _active_scheme


async def _get_persisted_scheme(session: AsyncSession) -> str | None:
    row = (await session.execute(select(AppSetting).where(AppSetting.key == _SETTING_KEY))).scalar_one_or_none()
    return row.value if row else None


async def set_auth_scheme(session: AsyncSession, scheme: str) -> None:
    """The only sanctioned way to change the persisted scheme after first
    boot — deliberately out-of-band (the `renderdesk set-auth-scheme` CLI
    command, run via container shell access), never triggered by just
    editing RENDERDESK_AUTH_SCHEME. See ensure_auth_scheme for why."""
    if scheme not in AUTH_SCHEMES:
        raise ValueError(f"invalid auth scheme {scheme!r}: must be one of {AUTH_SCHEMES}")
    existing = (await session.execute(select(AppSetting).where(AppSetting.key == _SETTING_KEY))).scalar_one_or_none()
    if existing is None:
        session.add(AppSetting(key=_SETTING_KEY, value=scheme))
    else:
        existing.value = scheme
    await session.commit()


async def ensure_auth_scheme(session: AsyncSession, configured_scheme: str) -> str:
    """Called once at startup, right after migrations run. First boot ever
    (nothing persisted yet): adopts and persists `configured_scheme` (from
    RENDERDESK_AUTH_SCHEME). Every boot after that: the env var must match
    what was persisted, or startup fails outright.

    This is deliberate friction: silently switching auth schemes by
    editing an env var is a real downgrade risk — e.g. falling back from
    SSO to local password auth for accounts that might not have a
    meaningful password set, or vice versa. Sets get_active_auth_scheme()'s
    return value as a side effect.
    """
    global _active_scheme
    persisted = await _get_persisted_scheme(session)
    if persisted is None:
        await set_auth_scheme(session, configured_scheme)
        persisted = configured_scheme
    elif persisted != configured_scheme:
        raise AuthSchemeMismatchError(
            f"RENDERDESK_AUTH_SCHEME is set to {configured_scheme!r}, but this instance was "
            f"already initialized with {persisted!r}. Changing the auth scheme requires the "
            f"deliberate `renderdesk set-auth-scheme {configured_scheme}` CLI command — it can't "
            f"be done by just editing the environment variable."
        )
    _active_scheme = persisted
    return persisted
