from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
APP_VERSION = "1.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173"

    database_url: str = f"sqlite:///{BACKEND_ROOT / 'storage' / 'merchant.db'}"
    store_name: str = "My Store"
    merchant_url: str = "http://localhost:8000"
    images_url: str = "http://localhost:8000/v1/images"

    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None

    rails_frontend_url: str = "http://localhost:3000"
    enable_scheduler: bool = True
    storage_root: str = str(BACKEND_ROOT / "storage")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def cors_origins_list() -> list[str]:
    return [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]


def is_production() -> bool:
    return get_settings().environment == "production"
