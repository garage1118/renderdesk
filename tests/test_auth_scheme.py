import pytest
from sqlalchemy import delete

import renderdesk.auth_scheme as auth_scheme
from renderdesk.auth_scheme import (
    AuthSchemeMismatchError,
    ensure_auth_scheme,
    get_active_auth_scheme,
    set_auth_scheme,
)
from renderdesk.db import session_scope
from renderdesk.models import AppSetting


async def _clear_persisted_scheme() -> None:
    # The _reset_auth_scheme autouse fixture already persists "password"
    # before every test — clear it to simulate a genuine first boot with
    # nothing persisted yet.
    async with session_scope() as session:
        await session.execute(delete(AppSetting).where(AppSetting.key == auth_scheme._SETTING_KEY))
        await session.commit()
    auth_scheme._active_scheme = None


async def test_first_boot_adopts_and_persists_the_configured_scheme():
    await _clear_persisted_scheme()

    async with session_scope() as session:
        resolved = await ensure_auth_scheme(session, "oidc")
    assert resolved == "oidc"
    assert get_active_auth_scheme() == "oidc"

    # A fresh process re-reading the same (now-initialized) DB sees the
    # persisted value regardless of what it's asked to adopt-if-unset.
    auth_scheme._active_scheme = None
    async with session_scope() as session:
        resolved_again = await ensure_auth_scheme(session, "oidc")
    assert resolved_again == "oidc"


async def test_matching_env_var_on_subsequent_boot_succeeds():
    async with session_scope() as session:
        await ensure_auth_scheme(session, "password")
    auth_scheme._active_scheme = None

    async with session_scope() as session:
        resolved = await ensure_auth_scheme(session, "password")
    assert resolved == "password"


async def test_mismatched_env_var_refuses_to_boot():
    async with session_scope() as session:
        await ensure_auth_scheme(session, "password")
    auth_scheme._active_scheme = None

    async with session_scope() as session:
        with pytest.raises(AuthSchemeMismatchError):
            await ensure_auth_scheme(session, "oidc")

    # The persisted value is untouched by the failed attempt.
    async with session_scope() as session:
        assert await auth_scheme._get_persisted_scheme(session) == "password"


async def test_set_auth_scheme_rejects_unknown_scheme():
    async with session_scope() as session:
        with pytest.raises(ValueError):
            await set_auth_scheme(session, "ldap")


async def test_set_auth_scheme_is_the_sanctioned_way_to_change_it():
    async with session_scope() as session:
        await ensure_auth_scheme(session, "password")

    # Deliberate change via the CLI-backed function, not by re-calling
    # ensure_auth_scheme with a different value (which would just error).
    async with session_scope() as session:
        await set_auth_scheme(session, "oidc")
    auth_scheme._active_scheme = None

    async with session_scope() as session:
        resolved = await ensure_auth_scheme(session, "oidc")
    assert resolved == "oidc"


def test_get_active_scheme_before_resolution_raises():
    auth_scheme._active_scheme = None
    with pytest.raises(RuntimeError):
        get_active_auth_scheme()
