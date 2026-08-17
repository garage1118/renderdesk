import time

import httpx
import pytest
import respx
from authlib.jose import JsonWebKey
from authlib.jose import jwt as authlib_jwt
from pydantic import ValidationError
from sqlalchemy import select

import renderdesk.auth_scheme as auth_scheme_module
from renderdesk import oidc
from renderdesk.app import app
from renderdesk.auth_scheme import ensure_auth_scheme, set_auth_scheme
from renderdesk.config import Settings, settings
from renderdesk.db import session_scope
from renderdesk.models import OidcIdentity, User
from renderdesk.oidc_state import read_cookie_value

from .conftest import make_user

ISSUER = "https://issuer.example/tenant"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
KID = "test-kid"

_DISCOVERY = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/authorize",
    "token_endpoint": f"{ISSUER}/token",
    "jwks_uri": f"{ISSUER}/jwks",
}


def _make_rsa_keypair():
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
    priv = key.as_dict(is_private=True)
    priv["kid"] = KID
    pub = key.as_dict(is_private=False)
    pub["kid"] = KID
    return JsonWebKey.import_key(priv), pub


def _sign_id_token(priv_key, kid: str = KID, omit: tuple[str, ...] = (), **claim_overrides) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "sub-1",
        "email": "person@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claim_overrides)
    for key in omit:
        payload.pop(key, None)
    token = authlib_jwt.encode({"alg": "RS256", "kid": kid}, payload, priv_key)
    return token.decode() if isinstance(token, bytes) else token


@pytest.fixture(autouse=True)
async def _oidc_scheme(monkeypatch):
    async with session_scope() as session:
        await set_auth_scheme(session, "oidc")
    auth_scheme_module._active_scheme = None
    async with session_scope() as session:
        await ensure_auth_scheme(session, "oidc")

    monkeypatch.setattr(settings, "oidc_issuer_url", ISSUER)
    monkeypatch.setattr(settings, "oidc_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "oidc_client_secret", CLIENT_SECRET)
    # Most tests here exercise the login flow itself, not the signup gate —
    # default it open and let the one test that cares about the gate
    # (test_new_user_signup_rejected_when_allow_signup_disabled) override it.
    monkeypatch.setattr(settings, "oidc_allow_signup", True)
    oidc._discovery_cache = None
    oidc._jwks_cache = None
    oidc._jwks_cache_at = 0.0
    yield


@pytest.fixture
def idp():
    priv_key, pub_jwk = _make_rsa_keypair()
    id_token_holder: dict[str, str] = {}

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{ISSUER}/.well-known/openid-configuration").respond(json=_DISCOVERY)
        mock.get(f"{ISSUER}/jwks").respond(json={"keys": [pub_jwk]})
        mock.post(f"{ISSUER}/token").mock(
            side_effect=lambda request: httpx.Response(
                200, json={"access_token": "at-1", "token_type": "Bearer", "id_token": id_token_holder["token"]}
            )
        )
        yield priv_key, id_token_holder


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


async def _start_login(client, next_path: str = "/dashboard") -> str:
    resp = await client.get("/dashboard/auth/oidc/login", params={"next": next_path})
    assert resp.status_code == 303
    raw_cookie = client.cookies["renderdesk_oidc_state"]
    flow = read_cookie_value(raw_cookie)
    assert flow is not None
    return flow["s"], flow["n"]


async def _callback(client, state: str | None, code: str | None = "fake-code", **extra_params):
    params = {}
    if code is not None:
        params["code"] = code
    if state is not None:
        params["state"] = state
    params.update(extra_params)
    return await client.get("/dashboard/auth/oidc/callback", params=params)


# --- happy paths --------------------------------------------------------


async def test_new_user_auto_provisioned_on_first_login(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, sub="sub-new", email="newperson@example.com")

    resp = await _callback(client, state)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/dashboard"
    assert "renderdesk_session" in resp.cookies

    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == "newperson@example.com"))).scalar_one()
        identity = (
            await session.execute(select(OidcIdentity).where(OidcIdentity.user_id == user.id))
        ).scalar_one()
        assert identity.issuer == ISSUER
        assert identity.subject == "sub-new"
        assert user.password_hash is None


async def test_second_login_same_subject_reuses_user_no_duplicate(client, idp):
    priv_key, id_token_holder = idp

    state1, nonce1 = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce1, sub="sub-repeat", email="repeat@example.com")
    resp1 = await _callback(client, state1)
    assert resp1.status_code == 303

    async with session_scope() as session:
        first_user_id = (
            await session.execute(select(User).where(User.email == "repeat@example.com"))
        ).scalar_one().id

    client.cookies.delete("renderdesk_session")
    state2, nonce2 = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce2, sub="sub-repeat", email="repeat@example.com")
    resp2 = await _callback(client, state2)
    assert resp2.status_code == 303

    async with session_scope() as session:
        users = (await session.execute(select(User).where(User.email == "repeat@example.com"))).scalars().all()
        assert len(users) == 1
        assert users[0].id == first_user_id
        identities = (
            await session.execute(select(OidcIdentity).where(OidcIdentity.subject == "sub-repeat"))
        ).scalars().all()
        assert len(identities) == 1


async def test_existing_password_user_with_matching_email_is_rejected_not_auto_linked(client, idp):
    # Auto-linking a first-time IdP login to an existing account by verified
    # email was deliberately removed (see the "harden auth surface" commit):
    # it let anyone who controls an IdP account's email claim silently take
    # over the matching local account. A collision must fail clean, with no
    # new identity/user created — manual linking is out of scope here.
    priv_key, id_token_holder = idp
    existing_user_id = await make_user(email="already-here@example.com", password="somepassword")

    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, sub="sub-linked", email="already-here@example.com")
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies
    assert "Contact an administrator" in resp.text

    async with session_scope() as session:
        identity = (
            await session.execute(select(OidcIdentity).where(OidcIdentity.subject == "sub-linked"))
        ).scalar_one_or_none()
        assert identity is None
        users = (
            await session.execute(select(User).where(User.email == "already-here@example.com"))
        ).scalars().all()
        assert len(users) == 1
        assert users[0].id == existing_user_id  # untouched, no duplicate created


async def test_new_user_signup_rejected_when_allow_signup_disabled(client, idp, monkeypatch):
    monkeypatch.setattr(settings, "oidc_allow_signup", False)
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, sub="sub-blocked", email="blocked@example.com")

    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies
    assert "Contact an administrator" in resp.text

    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == "blocked@example.com"))).scalar_one_or_none()
        assert user is None


async def test_missing_email_falls_back_to_preferred_username(client, idp):
    # Entra guest (B2B-invited) accounts frequently omit the email claim
    # even with the email scope granted — this is the real-world case that
    # first surfaced the gap.
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(
        priv_key, nonce=nonce, omit=("email",), preferred_username="guest_user@tenant.onmicrosoft.com"
    )
    resp = await _callback(client, state)
    assert resp.status_code == 303
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == "guest_user@tenant.onmicrosoft.com"))
        ).scalar_one()
        assert user is not None


async def test_missing_email_and_preferred_username_falls_back_to_upn(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(
        priv_key, nonce=nonce, omit=("email",), upn="someone@tenant.onmicrosoft.com"
    )
    resp = await _callback(client, state)
    assert resp.status_code == 303
    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == "someone@tenant.onmicrosoft.com"))
        ).scalar_one()
        assert user is not None


async def test_string_email_verified_false_does_not_count_as_verified(client, idp):
    # Regression for CLAUDE-SECURITY-RESULTS.md F15: bool("false") is True,
    # so a non-compliant IdP emitting the *string* "false" used to let an
    # explicitly-unverified email claim take the verified-email collision
    # path anyway — that path is what blocks silently pre-registering an
    # account keyed to someone else's address. A non-boolean claim must
    # fail the login outright rather than being silently coerced.
    priv_key, id_token_holder = idp
    existing_user_id = await make_user(email="already-here2@example.com", password="somepassword")

    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(
        priv_key, nonce=nonce, sub="sub-string-false", email="already-here2@example.com", email_verified="false"
    )
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies

    async with session_scope() as session:
        identity = (
            await session.execute(select(OidcIdentity).where(OidcIdentity.subject == "sub-string-false"))
        ).scalar_one_or_none()
        assert identity is None
        users = (
            await session.execute(select(User).where(User.email == "already-here2@example.com"))
        ).scalars().all()
        assert len(users) == 1
        assert users[0].id == existing_user_id


async def test_string_email_verified_true_also_rejected_as_non_boolean(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(
        priv_key, nonce=nonce, sub="sub-string-true", email="stringtrue@example.com", email_verified="true"
    )
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_no_email_or_fallback_claims_rejected(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, omit=("email",))
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_login_bounces_back_to_next_param(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client, next_path="/dashboard/connections")
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, email="nextparam@example.com")
    resp = await _callback(client, state)
    assert resp.headers["location"] == "/dashboard/connections"


# --- error paths ----------------------------------------------------------


async def test_wrong_nonce_rejected(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce="not-the-real-nonce")
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_wrong_audience_rejected(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, aud="someone-elses-client-id")
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_expired_id_token_rejected(client, idp):
    priv_key, id_token_holder = idp
    state, nonce = await _start_login(client)
    now = int(time.time())
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce, iat=now - 1000, exp=now - 500)
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_bad_signing_key_rejected(client, idp):
    other_priv_key, _ = _make_rsa_keypair()
    _, id_token_holder = idp
    state, nonce = await _start_login(client)
    # Signed with a key the mocked JWKS endpoint never advertised.
    id_token_holder["token"] = _sign_id_token(other_priv_key, nonce=nonce)
    resp = await _callback(client, state)
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_missing_state_cookie_rejected(client, idp):
    resp = await _callback(client, state="anything")
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_garbled_state_cookie_does_not_crash(client, idp):
    client.cookies.set("renderdesk_oidc_state", "not-a-valid-signed-cookie")
    resp = await _callback(client, state="anything")
    assert resp.status_code == 400


async def test_state_param_mismatch_rejected(client, idp):
    priv_key, id_token_holder = idp
    _state, nonce = await _start_login(client)
    id_token_holder["token"] = _sign_id_token(priv_key, nonce=nonce)
    resp = await _callback(client, state="totally-different-state")
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_idp_error_shows_clean_error_page(client, idp):
    await _start_login(client)
    resp = await client.get(
        "/dashboard/auth/oidc/callback", params={"error": "access_denied", "error_description": "user cancelled"}
    )
    assert resp.status_code == 400
    assert "renderdesk_session" not in resp.cookies


async def test_idp_error_description_is_never_reflected(client, idp):
    # Regression for CLAUDE-SECURITY-RESULTS.md F13: error/error_description
    # are attacker-controlled query params reachable with no cookie or
    # prior flow — the response must never interpolate them, only render a
    # fixed, allowlisted message.
    await _start_login(client)
    injected = "Your SSO session expired. Re-enter your password at https://evil.example"
    resp = await client.get(
        "/dashboard/auth/oidc/callback",
        params={"error": "access_denied", "error_description": injected},
    )
    assert resp.status_code == 400
    assert injected not in resp.text
    assert "evil.example" not in resp.text
    assert "Sign-in was cancelled at the identity provider." in resp.text


async def test_idp_unknown_error_code_falls_back_to_generic_message(client, idp):
    await _start_login(client)
    resp = await client.get(
        "/dashboard/auth/oidc/callback",
        params={"error": "<script>alert(1)</script>", "error_description": "whatever"},
    )
    assert resp.status_code == 400
    assert "<script>" not in resp.text
    assert "Sign-in was cancelled or failed at the identity provider." in resp.text


async def test_missing_authorization_code_rejected(client, idp):
    state, _nonce = await _start_login(client)
    resp = await _callback(client, state, code=None)
    assert resp.status_code == 400


# --- login_form / login_submit scheme gating -------------------------------


async def test_login_form_shows_sso_link_under_oidc_scheme(client, idp):
    resp = await client.get("/dashboard/login")
    assert resp.status_code == 200
    assert "Sign in with SSO" in resp.text
    assert 'name="password"' not in resp.text


async def test_login_submit_still_501s_under_oidc(client, idp):
    await client.get("/dashboard/login")  # seed a real CSRF cookie
    csrf_token = client.cookies["csrf_token"]
    resp = await client.post(
        "/dashboard/login", data={"email": "x@example.com", "password": "y", "csrf_token": csrf_token}
    )
    assert resp.status_code == 501


async def test_login_routes_still_501_under_saml(client):
    async with session_scope() as session:
        await set_auth_scheme(session, "saml")
    auth_scheme_module._active_scheme = None
    async with session_scope() as session:
        await ensure_auth_scheme(session, "saml")

    resp = await client.get("/dashboard/login")
    assert resp.status_code == 501

    resp2 = await client.get("/dashboard/auth/oidc/login")
    assert resp2.status_code == 501


# --- oidc_state.py round-trip ------------------------------------------------


def test_state_cookie_round_trip():
    from renderdesk.oidc_state import make_cookie_value

    value = make_cookie_value("s1", "n1", "v1", "/dashboard")
    decoded = read_cookie_value(value)
    assert decoded == {"s": "s1", "n": "n1", "v": "v1", "next": "/dashboard"}


def test_state_cookie_tampered_payload_rejected():
    from renderdesk.oidc_state import make_cookie_value

    value = make_cookie_value("s1", "n1", "v1", "/dashboard")
    body, sig = value.rsplit(".", 1)
    tampered = body[:-4] + "xxxx" + "." + sig
    assert read_cookie_value(tampered) is None


def test_state_cookie_tampered_signature_rejected():
    from renderdesk.oidc_state import make_cookie_value

    value = make_cookie_value("s1", "n1", "v1", "/dashboard")
    body, _sig = value.rsplit(".", 1)
    assert read_cookie_value(f"{body}.0" * 32) is None


@pytest.mark.parametrize("garbage", ["", "no-dot-at-all", "...", "abc.def.ghi"])
def test_state_cookie_garbage_input_does_not_raise(garbage):
    assert read_cookie_value(garbage) is None


# --- config.py validator -----------------------------------------------------


def test_settings_oidc_scheme_requires_all_three_fields():
    with pytest.raises(ValidationError) as exc_info:
        Settings(public_base_url="http://localhost:8000", auth_scheme="oidc")
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["loc"] == ()
    message = str(errors[0]["ctx"]["error"])
    assert "oidc_issuer_url" in message
    assert "oidc_client_id" in message
    assert "oidc_client_secret" in message


def test_settings_oidc_scheme_with_all_fields_succeeds():
    s = Settings(
        public_base_url="http://localhost:8000",
        auth_scheme="oidc",
        oidc_issuer_url=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_client_secret=CLIENT_SECRET,
    )
    assert s.auth_scheme == "oidc"


def test_settings_password_scheme_does_not_require_oidc_fields():
    s = Settings(public_base_url="http://localhost:8000", auth_scheme="password")
    assert s.oidc_issuer_url is None


def test_settings_rejects_wildcard_trusted_proxy_ips():
    # Regression for CLAUDE-SECURITY-RESULTS.md F8: '*' makes
    # X-Forwarded-For trivially attacker-spoofable, defeating every
    # IP-keyed rate limit.
    with pytest.raises(ValidationError) as exc_info:
        Settings(public_base_url="http://localhost:8000", auth_scheme="password", trusted_proxy_ips="*")
    message = str(exc_info.value.errors()[0]["ctx"]["error"])
    assert "trusted_proxy_ips" in message


def test_settings_accepts_a_real_trusted_proxy_ip():
    s = Settings(public_base_url="http://localhost:8000", auth_scheme="password", trusted_proxy_ips="10.0.0.5")
    assert s.trusted_proxy_ips == "10.0.0.5"
