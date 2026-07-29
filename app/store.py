"""Persistent supplier store (JSON on disk) with Fernet-encrypted tokens.

A *supplier* row = {key (adapter), name, base_url, enabled, note, token (encrypted),
tokenUpdated}. The token is the raw auth string the user pastes (e.g. the whole
`cookie` header) and is encrypted at rest. Kept in <cache_dir>/suppliers.json which
is gitignored (.cache/).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from app.config import get_settings

_lock = threading.Lock()


def _cache_dir() -> Path:
    p = Path(get_settings().cache_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _store_file() -> Path:
    return _cache_dir() / "suppliers.json"


# --- encryption -----------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet
    key = get_settings().fernet_key.strip()
    if not key:
        keyfile = _cache_dir() / "fernet.key"
        if keyfile.exists():
            key = keyfile.read_text(encoding="utf-8").strip()
        else:
            key = Fernet.generate_key().decode("utf-8")
            keyfile.write_text(key, encoding="utf-8")
    return Fernet(key.encode("utf-8"))


def _encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def _decrypt(token_enc: str) -> str:
    if not token_enc:
        return ""
    try:
        return _fernet().decrypt(token_enc.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


# --- persistence ----------------------------------------------------------
def _load() -> dict:
    fp = _store_file()
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data: dict) -> None:
    fp = _store_file()
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, fp)


def _public(row: dict) -> dict:
    """Supplier row without the secret token (adds hasToken). ``config`` (selectors,
    search URL, etc.) is not secret and is returned so the UI can edit generic sites."""
    return {
        "key": row["key"], "name": row.get("name"), "baseUrl": row.get("baseUrl"),
        "kind": row.get("kind", "builtin"), "config": row.get("config"),
        "enabled": row.get("enabled", True), "note": row.get("note", ""),
        "hasToken": bool(row.get("token")), "tokenUpdated": row.get("tokenUpdated"),
    }


# --- public API -----------------------------------------------------------
def list_suppliers() -> list[dict]:
    with _lock:
        data = _load()
    return [_public(r) for r in data.get("suppliers", [])]


def get_row(key: str) -> dict | None:
    with _lock:
        for r in _load().get("suppliers", []):
            if r["key"] == key:
                return r
    return None


def get_token(key: str) -> str:
    row = get_row(key)
    return _decrypt(row.get("token", "")) if row else ""


def upsert(key: str, *, name=None, base_url=None, enabled=None, note=None) -> dict:
    with _lock:
        data = _load()
        rows = data.setdefault("suppliers", [])
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            row = {"key": key, "kind": "builtin", "enabled": True, "token": "", "note": ""}
            rows.append(row)
        if name is not None:
            row["name"] = name
        if base_url is not None:
            row["baseUrl"] = base_url
        if enabled is not None:
            row["enabled"] = bool(enabled)
        if note is not None:
            row["note"] = note
        _save(data)
        return _public(row)


def upsert_site(key: str, name: str, base_url: str, config: dict, token: str | None = None) -> dict:
    """Create/update a user-defined generic site (kind='generic')."""
    with _lock:
        data = _load()
        rows = data.setdefault("suppliers", [])
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            row = {"key": key, "enabled": True, "token": "", "note": ""}
            rows.append(row)
        row["kind"] = "generic"
        row["name"] = name
        row["baseUrl"] = base_url
        row["config"] = config
        if token is not None and token.strip():
            row["token"] = _encrypt(token.strip())
            row["tokenUpdated"] = int(time.time())
        _save(data)
        return _public(row)


def set_token(key: str, token: str) -> dict:
    with _lock:
        data = _load()
        rows = data.setdefault("suppliers", [])
        row = next((r for r in rows if r["key"] == key), None)
        if row is None:
            row = {"key": key, "enabled": True, "note": ""}
            rows.append(row)
        row["token"] = _encrypt(token.strip())
        row["tokenUpdated"] = int(time.time())
        _save(data)
        return _public(row)


def ensure_seed(seed: list[dict]) -> None:
    """One-time seed of known suppliers (NO tokens) so a fresh install lists them ready
    for a token. Runs once (guarded by a flag); never overwrites existing rows/tokens,
    and user deletions stick."""
    with _lock:
        data = _load()
        if data.get("seeded"):
            return
        rows = data.setdefault("suppliers", [])
        existing = {r["key"] for r in rows}
        for sd in seed:
            if sd["key"] in existing:
                continue
            rows.append({"key": sd["key"], "kind": sd.get("kind", "builtin"),
                         "name": sd.get("name"), "baseUrl": sd.get("baseUrl"),
                         "config": sd.get("config"), "enabled": True, "token": "", "note": ""})
        data["seeded"] = True
        _save(data)


def delete(key: str) -> None:
    with _lock:
        data = _load()
        data["suppliers"] = [r for r in data.get("suppliers", []) if r["key"] != key]
        _save(data)
