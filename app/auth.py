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


# --- brute-force protection (in-memory, per IP) ---------------------------
# Nota: em memoria e por processo — com varios workers do gunicorn o limite efetivo
# e MAX * n_workers. Suficiente para um app pequeno atras do nginx.
_LOGIN_FAILS: dict[str, list[float]] = {}
_MAX_FAILS = 6        # tentativas erradas permitidas dentro da janela
_WINDOW = 300         # segundos (5 min): janela deslizante e duracao do bloqueio


def client_ip(request: Request) -> str:
    """IP do cliente — atras do nginx usa o 1o hop do X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def login_blocked(ip: str) -> int:
    """Segundos restantes de bloqueio (0 = liberado). Conta falhas na janela deslizante."""
    now = time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _WINDOW]
    _LOGIN_FAILS[ip] = fails
    if len(fails) >= _MAX_FAILS:
        oldest_relevant = sorted(fails)[len(fails) - _MAX_FAILS]
        return max(1, int(_WINDOW - (now - oldest_relevant)))
    return 0


def record_login_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(time.time())


def clear_login_fails(ip: str) -> None:
    _LOGIN_FAILS.pop(ip, None)


def require_auth(request: Request) -> None:
    """FastAPI dependency: 401 unless a valid session cookie is present."""
    if not valid_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="Não autenticado.")
