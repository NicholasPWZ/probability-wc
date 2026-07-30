"""Application configuration loaded from environment / .env."""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (…/config.py -> app/ -> project root). Anchor paths here so they are the
# SAME regardless of the process working directory (gunicorn vs CLI wrote to different
# .cache dirs when this was relative).
_ROOT = Path(__file__).resolve().parent.parent

# Camoufox stores its downloaded browser in the platformdirs user cache dir
# (XDG_CACHE_HOME/camoufox). Pin it to the project .cache on Linux so `camoufox fetch`
# and the running service (possibly different users/HOMEs) agree on the location — the
# dir is owned by the service user. Avoids "CamoufoxNotInstalled" at runtime.
if sys.platform.startswith("linux"):
    os.environ.setdefault("XDG_CACHE_HOME", str(_ROOT / ".cache"))

# Load .env into os.environ so per-supplier login creds (<KEY>_USER / <KEY>_PASS) are
# available for the auto-login (pydantic only reads the fixed fields below).
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:
    pass


def supplier_credentials(key: str) -> tuple[str | None, str | None]:
    """Login user/password for a supplier from .env, e.g. DIGIMACRO_USER / DIGIMACRO_PASS."""
    u = os.environ.get(f"{key.upper()}_USER")
    p = os.environ.get(f"{key.upper()}_PASS")
    return (u, p) if (u and p) else (None, None)


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
    keepalive_minutes: int = 12     # background ping to keep supplier sessions alive (0 disables)
    search_filter: bool = True      # drop supplier results that don't actually match the query
    autologin_enabled: bool = True  # when a session expires, try to re-login via Camoufox
    autologin_headless: str = ""    # "" = auto ("virtual" on Linux/Xvfb, True elsewhere)


@lru_cache
def get_settings() -> Settings:
    return Settings()
