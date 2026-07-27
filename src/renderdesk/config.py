from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RENDERDESK_", env_file=".env")

    database_path: str = "./data/renderdesk.db"
    # No default: this seeds every self-referencing URL the app generates
    # (artifact links, OAuth issuer/redirect URIs). A silent localhost
    # fallback in production would produce links that work for no one —
    # better to fail loudly at startup if it's unset.
    public_base_url: str
    token_expiry_days: int = 90

    max_artifacts_per_connection: int = 200
    max_bytes_per_artifact: int = 2_000_000
    max_total_bytes_per_connection: int = 50_000_000

    session_expiry_days: int = 30

    # Read only by the Stage 2 migration to seed the first User row — never
    # read by the running app itself, so it's fine to unset after that runs.
    admin_bootstrap_email: str | None = None
    admin_bootstrap_password: str | None = None


settings = Settings()
