"""Application configuration loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Single shared password to enter the app (empty = app is locked until set).
    app_password: str = ""
    # Signs the session cookie. Set a long random value in production.
    secret_key: str = "dev-insecure-change-me"
    # Fernet key (base64, 32 bytes) to encrypt stored supplier tokens. If empty, a key
    # is generated and persisted to <cache_dir>/fernet.key on first run.
    fernet_key: str = ""

    cache_dir: str = ".cache"
    request_timeout: int = 25       # per-request seconds for supplier scraping
    search_ttl: int = 120           # seconds to cache a (supplier, query) search result
    session_max_age: int = 30 * 24 * 3600  # app login cookie lifetime


@lru_cache
def get_settings() -> Settings:
    return Settings()
