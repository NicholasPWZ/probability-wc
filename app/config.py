"""Application configuration loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Gemini (optional)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Admin token required to change the Gemini key from the UI (leave empty to
    # disable UI editing). Set a long random value.
    admin_token: str = ""

    # Tournament (FotMob league id 77 = FIFA World Cup; season defaults to 2026)
    league_id: int = 77
    season: str = "2026"

    # Scraping
    recent_matches: int = 6
    max_concurrency: int = 4
    cache_ttl: int = 3600
    cache_dir: str = ".cache"

    # Display
    display_tz: str = "America/Sao_Paulo"

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
