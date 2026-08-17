from typing import Literal

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RENDERDESK_", env_file=".env")

    database_path: str = "./data/renderdesk.db"
    # No default: this seeds every self-referencing URL the app generates
    # (artifact links, OAuth issuer/redirect URIs). A silent localhost
    # fallback in production would produce links that work for no one —
    # better to fail loudly at startup if it's unset.
    public_base_url: str
    # No default, same reasoning as public_base_url — see auth_scheme.py
    # for what happens with this value at startup (it's locked in on first
    # boot and checked against the persisted value on every boot after).
    auth_scheme: Literal["password", "oidc", "saml"]
    token_expiry_days: int = 90

    max_artifacts_per_connection: int = 200
    max_bytes_per_artifact: int = 2_000_000
    max_total_bytes_per_connection: int = 50_000_000
    # Bounds the one table quotas.py's byte accounting didn't originally
    # cover (see DESIGN_NOTES.md / CLAUDE-SECURITY-RESULTS.md F3):
    # update_artifact appends a full content copy per call with no
    # automatic pruning, so an unbounded update loop could otherwise write
    # far more than max_total_bytes_per_connection actually allows once
    # version history is counted. Enforced at write time in
    # tools.update_artifact by deleting the oldest versions beyond this
    # count, inside the same quota_lock-held transaction as the write.
    max_versions_per_artifact: int = 20

    session_expiry_days: int = 30

    # Read only by the Stage 2 migration to seed the first User row — never
    # read by the running app itself, so it's fine to unset after that runs.
    admin_bootstrap_email: str | None = None
    admin_bootstrap_password: str | None = None

    # Required only when auth_scheme == "oidc" (see the validator below) —
    # see oidc.py for how these drive discovery/token-exchange/redirect_uri.
    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    # Off by default: accounts are otherwise only ever provisioned
    # out-of-band via `renderdesk create-user` (see cli.py) — a successful
    # login at the IdP shouldn't silently double as a signup flow unless
    # explicitly opted into.
    oidc_allow_signup: bool = False

    @model_validator(mode="after")
    def _require_oidc_settings_when_active(self) -> "Settings":
        if self.auth_scheme == "oidc":
            missing = [
                name
                for name in ("oidc_issuer_url", "oidc_client_id", "oidc_client_secret")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(", ".join(missing))
        return self


try:
    settings = Settings()
except ValidationError as exc:
    # Pydantic's default rendering dumps the entire raw input mapping as
    # context on every error, which would print any already-set secrets
    # (e.g. admin_bootstrap_password) in plaintext to the container logs
    # just because some unrelated required field was missing. Only the
    # field names are safe to surface. A model_validator's plain ValueError
    # (see _require_oidc_settings_when_active) has an empty loc — its
    # message is the one thing we deliberately put there ourselves, so it's
    # already safe to show as-is.
    missing = ", ".join(str(err["loc"][0]) if err["loc"] else str(err["ctx"]["error"]) for err in exc.errors())
    raise SystemExit(
        f"renderdesk failed to start: missing or invalid required settings ({missing}). "
        "See .env.example for what's required."
    ) from None
