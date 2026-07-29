"""Single-password gate with a signed session cookie (stdlib HMAC — no extra deps)."""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from app.config import get_settings

COOKIE_NAME = "wp_session"


def _sign(msg: str) -> str:
    key = get_settings().secret_key.encode("utf-8")
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session() -> str:
    """Signed token: '<issued_ts>.<hmac>' — proves the user knew the password."""
    ts = str(int(time.time()))
    return f"{ts}.{_sign('authed:' + ts)}"


def valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    ts, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign("authed:" + ts)):
        return False
    try:
        return (time.time() - int(ts)) <= get_settings().session_max_age
    except ValueError:
        return False


def check_password(pw: str) -> bool:
    real = get_settings().app_password
    return bool(real) and hmac.compare_digest(pw or "", real)


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 unless a valid session cookie is present."""
    if not valid_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Não autenticado.")
