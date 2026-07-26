from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RENDERDESK_", env_file=".env")

    database_path: str = "./data/renderdesk.db"
    public_base_url: str = "http://localhost:8000"
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
