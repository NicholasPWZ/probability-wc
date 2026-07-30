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
    "multimarcas": {  # Tray, login SPA em 2 etapas
        "loginUrl": "https://www.multimarcasdistribuidora.com.br/my-account/login",
        "reveal": "text=Entrar",            # abre o form
        "user": "#input-email", "userSubmit": "Enter",   # etapa 1 -> revela a senha
        "pw": "#input-password", "submit": "#password-submit",
        "domain": "multimarcasdistribuidora.com.br",
        "success": lambda page: True,
    },
    "braile": {  # WMW SPA, login num modal (abre por um trigger no header)
        "loginUrl": "https://www.brailedistribuidora.com.br/",
        # o click do Playwright nao abre o modal de forma confiavel; um .click() nativo no
        # gatilho visivel abre. Usa JS.
        "revealJs": """const e=[...document.querySelectorAll("[ng-click*='openCloseMenuUser']")].find(x=>x.offsetParent!==null); if(e) e.click();""",
        "user": "#user:visible", "pw": "#ipt-password:visible",
        "submitJs": """const b=[...document.querySelectorAll("button[ng-click*='vm.login']")].find(x=>x.offsetParent!==null); if(b) b.click();""",
        "domain": "brailedistribuidora.com.br",
        "success": lambda page: True,
    },
}


def _dismiss_cookie(page) -> None:
    """Best-effort: fecha banner de cookies que pode bloquear cliques."""
    for txt in ("EU ACEITO", "Aceito", "Aceitar", "Aceitar cookies", "Accept", "Entendi", "OK, entendi"):
        try:
            el = page.query_selector(f"text={txt}")
            if el and el.is_visible():
                el.click(timeout=3000)
                return
        except Exception:
            pass

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
        try:
            page.wait_for_load_state("networkidle", timeout=20000)  # deixa o SPA bootar
        except Exception:
            pass
        _dismiss_cookie(page)
        reveal, reveal_js = recipe.get("reveal"), recipe.get("revealJs")
        if reveal or reveal_js:
            # SPA flaky: repete o gatilho ate o campo de usuario ficar visivel. `revealJs` faz
            # um .click() nativo (mais confiavel que o click do Playwright em alguns SPAs).
            for _ in range(4):
                try:
                    if reveal_js:
                        page.evaluate("() => { " + reveal_js + " }")
                    else:
                        for el in page.query_selector_all(reveal):
                            try:
                                el.click(force=True, timeout=4000)
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    page.wait_for_selector(recipe["user"], state="visible", timeout=5000)
                    break
                except Exception:
                    page.wait_for_timeout(1000)
        else:
            try:
                page.wait_for_selector(recipe["user"], state="visible", timeout=15000)
            except Exception:
                pass
        page.fill(recipe["user"], user, timeout=20000)
        us = recipe.get("userSubmit")           # two-step logins: advance after the user field
        if us:
            if us == "Enter":
                page.press(recipe["user"], "Enter")
            else:
                page.click(us)
            try:
                page.wait_for_selector(recipe["pw"], timeout=15000)   # wait for the password step
            except Exception:
                pass
        page.fill(recipe["pw"], pwd, timeout=20000)
        if recipe.get("submitJs"):
            page.evaluate("() => { " + recipe["submitJs"] + " }")   # click nativo (ignora overlays)
        elif recipe.get("submit"):
            page.click(recipe["submit"], force=True)   # force: ignora overlays (ex.: popup)
        else:
            page.press(recipe["pw"], "Enter")   # standard form submit
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(5000)   # deixa o login assincrono (SPA/XHR) concluir e o cookie virar "logado"
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
