from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    oreshnik_env: str = "dev"
    secret_encryption_key: str = "CHANGE_ME_generate_a_fernet_key"
    jwt_secret: str = "CHANGE_ME_random_hex"
    jwt_expire_minutes: int = 720
    jwt_algorithm: str = "HS256"

    # Database
    postgres_user: str = "oreshnik"
    postgres_password: str = "oreshnik"
    postgres_db: str = "oreshnik"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Redis / Celery
    redis_url: str = "redis://redis:6379/0"

    # Runner / Strix
    strix_runs_dir: str = "/data/strix_runs"
    default_runner: str = "mock"

    # CORS
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
