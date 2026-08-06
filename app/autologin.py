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
    "agis": {  # Magento B2B (vendas.agis.com.br); login na pagina dedicada. Campo do usuario e
        # #login (name=login[username]); #email na pagina e do newsletter. Botao #send2 ("Entre").
        "loginUrl": "https://vendas.agis.com.br/customer/account/login/",
        "user": "#login", "pw": "#pass", "submit": "#send2",
        "domain": "vendas.agis.com.br",
        "success": lambda page: "/customer/account/login" not in (page.url or ""),
    },
    "reatacado": {  # OpenCart: login por HTTP (curl_cffi), NAO Camoufox — o Firefox do Camoufox toma
        # NS_ERROR_NET_EMPTY_RESPONSE (anti-bot derruba a conexao); curl_cffi impersonate=chrome passa.
        # POST simples email/password, sem captcha. Sucesso = saiu de account/login.
        "http": True,
        "loginUrl": "https://www.reatacado.com.br/index.php?route=account/login",
        "userField": "email", "passField": "password",
        "domain": "reatacado.com.br",
        # logado = a pagina tem link de logout (guest so tem login/register). Robusto (independe de redirect).
        "success": lambda url, html: "account/logout" in (html or "").lower(),
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


_UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36")


def _http_login(key: str, recipe: dict, user: str, pwd: str) -> str | None:
    """Login SEM navegador (curl_cffi impersonate=chrome) — p/ sites cujo anti-bot derruba o Camoufox
    mas aceitam o fingerprint TLS do Chrome. GET a pagina de login (pega cookies/hidden fields), POST
    email/senha, valida via recipe['success'](url, html). Retorna o cookie jar so em caso de sucesso."""
    from urllib.parse import urlparse
    from bs4 import BeautifulSoup
    from curl_cffi import requests as creq
    s = creq.Session(impersonate="chrome", timeout=get_settings().request_timeout)
    s.headers.update({"user-agent": _UA_CHROME, "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
                      "accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    login_url = recipe["loginUrl"]
    g = s.get(login_url)
    # monta os dados do form (inclui hidden/CSRF, se houver, p/ robustez entre versoes)
    data: dict = {}
    action = login_url
    try:
        forms = [f for f in BeautifulSoup(g.text, "html.parser").find_all("form")
                 if f.find("input", {"type": "password"})]
        if forms:
            action = forms[0].get("action") or login_url
            for inp in forms[0].find_all("input"):
                n = inp.get("name")
                if n and inp.get("type") != "submit":
                    data[n] = inp.get("value") or ""
    except Exception:
        pass
    data[recipe.get("userField", "email")] = user
    data[recipe.get("passField", "password")] = pwd
    u = urlparse(login_url)
    r = s.post(action, data=data, allow_redirects=True,
               headers={"referer": login_url, "origin": f"{u.scheme}://{u.netloc}",
                        "content-type": "application/x-www-form-urlencoded"})
    # confirma o login na pagina de conta (mais confiavel que a URL: independe de seguir o 302)
    acc = None
    try:
        acc = s.get(f"{u.scheme}://{u.netloc}/index.php?route=account/account")
    except Exception:
        pass
    chk_url = (acc.url if acc is not None else r.url) or ""
    chk_html = (acc.text if acc is not None else r.text) or ""
    try:
        ok = bool(recipe["success"](chk_url, chk_html))
    except Exception:
        ok = True
    if ok:
        _last_error.pop(key, None)
    else:
        msg = ""
        try:
            for sel in (".alert-danger", ".alert", ".text-danger", ".warning"):
                el = BeautifulSoup(r.text, "html.parser").select_one(sel)
                if el and el.get_text(strip=True):
                    msg = el.get_text(" ", strip=True)[:140]
                    break
        except Exception:
            pass
        _last_error[key] = msg or "login nao confirmado"
        return None   # nao persiste cookie de visitante
    jar = "; ".join(f"{k}={v}" for k, v in s.cookies.items() if v is not None)
    return jar or None


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
# ultimo motivo de falha por fornecedor (p/ a UI mostrar algo util em vez de "veja os logs")
_last_error: dict[str, str] = {}


def last_error(key: str) -> str:
    return _last_error.get(key, "")


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
        _last_error[key] = "sem credenciais no .env"
        return None
    # receita HTTP (curl_cffi, sem navegador) — p/ sites que bloqueiam o Camoufox
    if recipe.get("http"):
        lock = _lock(key)
        if not lock.acquire(blocking=False):
            return None
        try:
            return _http_login(key, recipe, user, pwd)
        except Exception as exc:
            print(f"[autologin] {key} (http) falhou: {exc}")
            _last_error[key] = f"erro no login: {exc}"
            return None
        finally:
            lock.release()
    lock = _lock(key)
    if not lock.acquire(blocking=False):
        return None  # a login is already in progress for this supplier
    try:
        try:
            from camoufox.sync_api import Camoufox  # noqa: F401
        except Exception as exc:
            print(f"[autologin] Camoufox indisponivel: {exc}")
            _last_error[key] = f"Camoufox indisponivel: {exc}"
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
                    _last_error[key] = f"erro no login: {exc2}"
                    return None
            print(f"[autologin] {key} falhou: {exc}")
            _last_error[key] = f"erro no login: {exc}"
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
        if ok:
            _last_error.pop(key, None)
        else:
            # captura a mensagem de erro do site (ex.: Magento "Incorrect CAPTCHA") p/ a UI
            msg = ""
            for sel in ("[data-ui-id*=error]", ".message-error", ".messages"):
                try:
                    el = page.query_selector(sel)
                    if el:
                        t = (el.inner_text() or "").strip()
                        if t:
                            msg = t.splitlines()[0][:140]
                            break
                except Exception:
                    pass
            _last_error[key] = msg or "login nao confirmado (ainda na pagina de login)"
        cookies = page.context.cookies()
    if not ok:
        print(f"[autologin] {key}: login nao confirmado ({_last_error.get(key, '')})")
    jar = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                    if recipe["domain"] in (c.get("domain") or ""))
    return jar or None
