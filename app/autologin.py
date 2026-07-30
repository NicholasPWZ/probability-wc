"""Auto-login via Camoufox (anti-detect Firefox) to re-mint an expired supplier session.

When a supplier session is detected expired (keep-alive / on demand), this launches
Camoufox headless (``virtual``/Xvfb on the VPS), logs in with the supplier's `.env`
credentials, and returns the fresh cookie string. Logging in FROM the server also means
the session is issued to the server's IP — sidestepping IP-bound sessions. Runs rarely
(only on expiry) and one login per supplier at a time.
"""
from __future__ import annotations

import sys
import threading

from app.config import get_settings, supplier_credentials

# Per-supplier login recipe. Keys: loginUrl, user, pw (CSS selectors); optional `reveal`
# (selector to click first to show the form) and `submit` (button selector; if omitted,
# presses Enter on the password field). `domain` filters the cookies to keep. The final
# authority on success is the adapter's check_auth in the caller (a returned cookie is
# validated before being persisted), so `success` here is just a best-effort hint.
RECIPES: dict[str, dict] = {
    "digimacro": {
        "loginUrl": "https://digimacro.com.br/acesso",
        "user": "input[name=email]", "pw": "input[name=password]", "submit": "#submit-login",
        "domain": "digimacro.com.br",
        "success": lambda page: "/acesso" not in (page.url or ""),
    },
    "pauta": {
        "loginUrl": "https://pauta.com.br/login",
        # nopCommerce login por CNPJ; a pagina tem 3 forms iguais -> mira o VISIVEL. Submit via Enter.
        "user": "#Email:visible", "pw": "#Password:visible",
        "domain": "pauta.com.br",
        "success": lambda page: "/login" not in (page.url or ""),
    },
    "mazer": {
        "loginUrl": "https://www.mazer.com.br/",  # form visivel na home (contentLoginHome)
        # #enter fica fora da viewport (nao clica) -> submete via Enter
        "user": "#username:visible", "pw": "#password:visible",
        "domain": "mazer.com.br",
        "success": lambda page: True,
    },
}

_locks: dict[str, threading.Lock] = {}


def _lock(key: str) -> threading.Lock:
    return _locks.setdefault(key, threading.Lock())


def _headless_mode():
    h = (get_settings().autologin_headless or "").strip().lower()
    if h == "true":
        return True
    if h == "false":
        return False
    if h == "virtual":
        return "virtual"
    # auto: Xvfb-backed "virtual" on Linux (best stealth), native headless elsewhere
    return "virtual" if sys.platform.startswith("linux") else True


def can_autologin(key: str) -> bool:
    if key not in RECIPES:
        return False
    u, p = supplier_credentials(key)
    return bool(u and p)


def login(key: str, watch: bool = False) -> str | None:
    """Log in with Camoufox and return the fresh cookie string (or None on failure).
    ``watch=True`` shows the browser (local debugging)."""
    recipe = RECIPES.get(key)
    if not recipe:
        return None
    user, pwd = supplier_credentials(key)
    if not (user and pwd):
        print(f"[autologin] {key}: sem credenciais no .env ({key.upper()}_USER/_PASS)")
        return None
    lock = _lock(key)
    if not lock.acquire(blocking=False):
        return None  # a login is already in progress for this supplier
    try:
        try:
            from camoufox.sync_api import Camoufox  # noqa: F401
        except Exception as exc:
            print(f"[autologin] Camoufox indisponivel: {exc}")
            return None
        mode = False if watch else _headless_mode()
        try:
            return _run_login(key, recipe, user, pwd, mode)
        except Exception as exc:
            # "virtual" needs the Xvfb binary; if it's missing, fall back to native headless
            from camoufox.exceptions import VirtualDisplayError
            if mode == "virtual" and isinstance(exc, VirtualDisplayError):
                print(f"[autologin] {key}: Xvfb indisponivel ({exc}); tentando headless nativo")
                try:
                    return _run_login(key, recipe, user, pwd, True)
                except Exception as exc2:
                    print(f"[autologin] {key} falhou (nativo): {exc2}")
                    return None
            print(f"[autologin] {key} falhou: {exc}")
            return None
    finally:
        lock.release()


def _run_login(key, recipe, user, pwd, headless) -> str | None:
    from camoufox.sync_api import Camoufox
    with Camoufox(headless=headless) as browser:
        page = browser.new_page()
        page.goto(recipe["loginUrl"], wait_until="domcontentloaded", timeout=45000)
        if recipe.get("reveal"):
            try:
                page.click(recipe["reveal"], timeout=8000)
            except Exception:
                pass
        page.fill(recipe["user"], user, timeout=20000)
        page.fill(recipe["pw"], pwd, timeout=20000)
        if recipe.get("submit"):
            page.click(recipe["submit"])
        else:
            page.press(recipe["pw"], "Enter")   # standard form submit
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        try:
            ok = bool(recipe["success"](page))
        except Exception:
            ok = True
        cookies = page.context.cookies()
    if not ok:
        print(f"[autologin] {key}: login nao confirmado (ainda em {recipe['loginUrl']})")
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if recipe["domain"] in (c.get("domain") or ""))
    return jar or None
