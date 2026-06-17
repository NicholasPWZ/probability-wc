"""Runtime-editable config (overrides .env), persisted to disk.

Lets the Gemini API key/model be changed from the UI (guarded by ADMIN_TOKEN)
without redeploying. Stored in <cache_dir>/runtime.json. The key is never
returned to clients.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import get_settings

_lock = threading.Lock()


def _file() -> Path:
    return Path(get_settings().cache_dir) / "runtime.json"


def _load() -> dict:
    fp = _file()
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def gemini_api_key() -> str:
    return (_load().get("gemini_api_key") or get_settings().gemini_api_key or "").strip()


def gemini_model() -> str:
    return (_load().get("gemini_model") or get_settings().gemini_model or "").strip()


def gemini_enabled() -> bool:
    return bool(gemini_api_key())


def set_gemini(api_key: str | None = None, model: str | None = None) -> None:
    with _lock:
        data = _load()
        if api_key is not None:
            data["gemini_api_key"] = api_key.strip()
        if model:
            data["gemini_model"] = model.strip()
        fp = _file()
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(data), encoding="utf-8")
