"""Application configuration loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (…/config.py -> app/ -> project root). Anchor paths here so they are the
# SAME regardless of the process working directory (gunicorn vs CLI wrote to different
# .cache dirs when this was relative).
_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ROOT / ".env"),
                                      env_file_encoding="utf-8", extra="ignore")

    # Single shared password to enter the app (empty = app is locked until set).
    app_password: str = ""
    # Signs the session cookie. Set a long random value in production.
    secret_key: str = "dev-insecure-change-me"
    # Fernet key (base64, 32 bytes) to encrypt stored supplier tokens. If empty, a key
    # is generated and persisted to <cache_dir>/fernet.key on first run.
    fernet_key: str = ""

    # Absolute so gunicorn and any CLI/one-off always use the same store.
    cache_dir: str = str(_ROOT / ".cache")
    request_timeout: int = 25       # per-request seconds for supplier scraping
    search_ttl: int = 120           # seconds to cache a (supplier, query) search result
    session_max_age: int = 30 * 24 * 3600  # app login cookie lifetime


@lru_cache
def get_settings() -> Settings:
    return Settings()
