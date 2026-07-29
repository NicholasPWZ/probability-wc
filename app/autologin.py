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

# Per-supplier login recipe. Add one entry per site whose login we automate.
RECIPES: dict[str, dict] = {
    "digimacro": {
        "loginUrl": "https://digimacro.com.br/acesso",
        "user": "input[name=email]",
        "pw": "input[name=password]",
        "submit": "#submit-login",
        "domain": "digimacro.com.br",
        # PrestaShop leaves /acesso once logged in
        "success": lambda page: "/acesso" not in (page.url or ""),
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
            from camoufox.sync_api import Camoufox
        except Exception as exc:
            print(f"[autologin] Camoufox indisponivel: {exc}")
            return None
        headless = False if watch else _headless_mode()
        with Camoufox(headless=headless) as browser:
            page = browser.new_page()
            page.goto(recipe["loginUrl"], wait_until="domcontentloaded", timeout=45000)
            page.fill(recipe["user"], user)
            page.fill(recipe["pw"], pwd)
            page.click(recipe["submit"])
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
    except Exception as exc:
        print(f"[autologin] {key} falhou: {exc}")
        return None
    finally:
        lock.release()
