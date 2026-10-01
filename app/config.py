from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    # Works whether uvicorn is started from backend/ or from the repo root
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./library.db"
    secret_key: str = "dev-only-change-me"
    cors_origins: str = "http://localhost:5173"

    bootstrap_admin_id: str = "ADMIN001"
    bootstrap_admin_password: str = "change-me-now"
    bootstrap_admin_email: str = ""
    seed_demo_data: bool = False
    demo_password: str = "Demo@1234"
    run_scheduler: bool = True

    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    grok_model: str = "grok-4.3"
    grok_timeout_seconds: int = 45

    pinecone_api_key: str = ""
    pinecone_index: str = "nitt-library"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "library@nitt.edu.ng"

    @property
    def db_url(self) -> str:
        url = self.database_url
        # Hosted Postgres providers hand out postgres:// or postgresql:// URLs
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_config() -> Config:
    return Config()
