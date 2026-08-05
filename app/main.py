"""FastAPI app: reseller multi-supplier price search.

Single-password gate → manage supplier auth tokens → free-text search fanned out
across all enabled suppliers, prices side by side. Backend does the scraping in
Python (per-supplier adapters). Served at the domain root.
"""
from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _FuturesTimeout
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import auth, pdf, quotes, store, suppliers
from app.config import get_settings
from app.models import (LoginRequest, QuoteRequest, SiteConfig, SiteTest,
                        SupplierUpsert, TokenRequest)
from app.suppliers.generic import GenericAdapter


def _resolve_adapter(key: str):
    """Return the adapter for a supplier key — a user-defined generic site (from its
    stored config) or a built-in code adapter."""
    row = store.get_row(key)
    if row and row.get("kind") == "generic":
        return GenericAdapter(key, row.get("name"), row.get("baseUrl"), row.get("config") or {})
    return suppliers.get_adapter(key)

STATIC_DIR = Path(__file__).parent / "static"

compubot = FastAPI(title="Compubot — Comparador de Fornecedores", version="1.0")
compubot.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@compubot.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------
@compubot.get("/api/config")
async def api_config(request: Request):
    return {
        "authed": auth.valid_session(request.cookies.get(auth.COOKIE_NAME)),
        "appConfigured": bool(get_settings().app_password),
        "company": get_settings().company_name or "CompuJob",   # default editavel do nome da empresa
    }


@compubot.post("/api/login")
async def api_login(req: LoginRequest, request: Request, response: Response):
    if not get_settings().app_password:
        raise HTTPException(status_code=503, detail="App sem senha definida (APP_PASSWORD no .env).")
    ip = auth.client_ip(request)
    wait = auth.login_blocked(ip)
    if wait:
        raise HTTPException(status_code=429,
                            detail=f"Muitas tentativas. Tente novamente em ~{wait // 60 + 1} min.")
    if not auth.check_password(req.password):
        auth.record_login_fail(ip)
        raise HTTPException(status_code=401, detail="Senha incorreta.")
    auth.clear_login_fails(ip)
    response.set_cookie(
        auth.COOKIE_NAME, auth.make_session(),
        max_age=get_settings().session_max_age, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return {"ok": True}


@compubot.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


# --------------------------------------------------------------------------
# suppliers
# --------------------------------------------------------------------------
@compubot.get("/api/adapters", dependencies=[Depends(auth.require_auth)])
async def api_adapters():
    """Available supplier adapters the user can add."""
    return {"adapters": [{"key": a.key, "name": a.name, "baseUrl": a.base_url,
                          "authHelp": a.auth_help} for a in suppliers.all_adapters()]}


@compubot.get("/api/suppliers", dependencies=[Depends(auth.require_auth)])
async def api_suppliers():
    from app import autologin
    rows = store.list_suppliers()
    for r in rows:
        r["canAutologin"] = autologin.can_autologin(r["key"])
    return {"suppliers": rows}


@compubot.put("/api/suppliers", dependencies=[Depends(auth.require_auth)])
async def api_supplier_upsert(req: SupplierUpsert):
    builtin = suppliers.get_adapter(req.key)
    if builtin is None and store.get_row(req.key) is None:
        raise HTTPException(status_code=400, detail=f"Fornecedor desconhecido: {req.key}")
    return store.upsert(req.key,
                        name=req.name or (builtin.name if builtin else None),
                        base_url=req.baseUrl or (builtin.base_url if builtin else None),
                        enabled=req.enabled, note=req.note)


@compubot.post("/api/suppliers/{key}/token", dependencies=[Depends(auth.require_auth)])
async def api_supplier_token(key: str, req: TokenRequest):
    builtin = suppliers.get_adapter(key)
    if builtin is None and store.get_row(key) is None:
        raise HTTPException(status_code=400, detail=f"Fornecedor desconhecido: {key}")
    if builtin is not None and store.get_row(key) is None:
        store.upsert(key, name=builtin.name, base_url=builtin.base_url)   # first token for a builtin
    return store.set_token(key, req.token)


# --------------------------------------------------------------------------
# user-defined generic sites (configured from the UI, no code)
# --------------------------------------------------------------------------
def _site_config(req: SiteConfig) -> dict:
    return {"searchUrl": req.searchUrl, "authMode": req.authMode, "priceLocale": req.priceLocale,
            "selectors": req.selectors or {}, "linkAttr": req.linkAttr, "imageAttr": req.imageAttr}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", (s or "").strip().lower().replace(" ", "-"))


@compubot.post("/api/sites", dependencies=[Depends(auth.require_auth)])
async def api_site_upsert(req: SiteConfig):
    key = _slug(req.key or req.name)
    if not key:
        raise HTTPException(status_code=400, detail="Informe um identificador (key) ou nome.")
    if suppliers.get_adapter(key) is not None:
        raise HTTPException(status_code=400, detail=f"'{key}' é um adapter embutido — escolha outro id.")
    if "{q}" not in (req.searchUrl or ""):
        raise HTTPException(status_code=400, detail="A URL de busca precisa conter {q}.")
    return store.upsert_site(key, req.name, req.baseUrl, _site_config(req), token=req.token)


@compubot.post("/api/sites/test", dependencies=[Depends(auth.require_auth)])
async def api_site_test(req: SiteTest):
    if "{q}" not in (req.searchUrl or ""):
        raise HTTPException(status_code=400, detail="A URL de busca precisa conter {q}.")
    key = _slug(req.key or req.name) or "test"
    adapter = GenericAdapter(key, req.name or key, req.baseUrl, _site_config(req))
    token = (req.token or "").strip() or store.get_token(key)

    def _run():
        session = adapter.build_session(token)
        return [p.to_dict() for p in adapter.search(req.query, session)]

    try:
        products = await run_in_threadpool(_run)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao testar: {exc}")
    priced = sum(1 for p in products if p.get("price") is not None)
    return {"count": len(products), "priced": priced, "products": products[:15]}


@compubot.post("/api/suppliers/{key}/test", dependencies=[Depends(auth.require_auth)])
async def api_supplier_test(key: str):
    adapter = _resolve_adapter(key)
    if adapter is None:
        raise HTTPException(status_code=400, detail=f"Adapter desconhecido: {key}")
    token = store.get_token(key)
    if not token:
        return {"ok": False, "authed": False, "detail": "Nenhum token salvo."}

    def _test():
        session = adapter.build_session(token)
        authed = adapter.check_auth(session)
        refreshed = adapter.dump_token(session)
        if refreshed and refreshed != token:
            store.set_token(key, refreshed)   # persist a renewed (sliding) session
        return authed

    try:
        authed = await run_in_threadpool(_test)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao testar: {exc}")
    return {"ok": True, "authed": authed,
            "detail": "Sessão válida." if authed else "Sessão expirada — atualize o token."}


@compubot.delete("/api/suppliers/{key}", dependencies=[Depends(auth.require_auth)])
async def api_supplier_delete(key: str):
    store.delete(key)
    return {"ok": True}


@compubot.post("/api/suppliers/{key}/relogin", dependencies=[Depends(auth.require_auth)])
async def api_supplier_relogin(key: str):
    """Force a fresh login via Camoufox (uses .env credentials)."""
    from app import autologin
    if not autologin.can_autologin(key):
        raise HTTPException(status_code=400,
                            detail="Sem auto-login p/ este fornecedor (defina "
                                   f"{key.upper()}_USER/_PASS no .env e a receita de login).")
    tok = await run_in_threadpool(_autologin_refresh, key)
    detail = "Sessão renovada via login."
    if not tok:
        reason = autologin.last_error(key)
        detail = (f"Falha no auto-login: {reason}" if reason
                  else "Falha no auto-login (veja os logs).")
    return {"ok": bool(tok), "authed": bool(tok), "detail": detail}


# --------------------------------------------------------------------------
# search (fan out across enabled suppliers)
# --------------------------------------------------------------------------
_search_cache: dict[tuple, tuple] = {}   # (key, q) -> (ts, products)


def _norm(s: str) -> str:
    """lowercase + strip accents (para casar 'video' com 'vídeo')."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _query_tokens(q: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", _norm(q)) if len(t) >= 3]


def _is_relevant(tokens: list[str], p: dict) -> bool:
    """True se o produto casa com o termo: todos os tokens (>=3 chars) aparecem no
    nome/marca/SKU/part-number. Corrige buscadores que devolvem 'destaques' quando nao acham."""
    if not tokens:
        return True
    hay = _norm(" ".join(str(p.get(k) or "") for k in ("name", "brand", "sku", "part_number")))
    return all(t in hay for t in tokens)


def _row_no_auth(row: dict | None) -> bool:
    """A generic site configured authMode=none needs no token (prices are public)."""
    return bool(row) and (str((row.get("config") or {}).get("authMode", "")).lower() == "none")


def _supplier_ready(row: dict) -> bool:
    """Searchable = enabled AND (has a token OR needs no auth). Aceita linha publica
    (`hasToken`, de list_suppliers) OU crua (`token`, de get_row)."""
    has_token = bool(row.get("hasToken") or row.get("token"))
    return bool(row.get("enabled")) and (has_token or _row_no_auth(row))


def _request_timeouts() -> dict[str, int]:
    """Override de timeout por loja: REQUEST_TIMEOUT_OVERRIDES='chave:seg,chave:seg'."""
    out: dict[str, int] = {}
    for part in (get_settings().request_timeout_overrides or "").split(","):
        part = part.strip()
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                out[k.strip()] = int(v)
            except ValueError:
                pass
    return out


def _search_one(key: str, query: str) -> dict:
    adapter = _resolve_adapter(key)
    if adapter is None:
        return {"key": key, "ok": False, "error": "adapter ausente", "products": []}
    token = store.get_token(key)
    if not token and not _row_no_auth(store.get_row(key)):
        return {"key": key, "name": adapter.name, "ok": False,
                "error": "sem token — configure em Fornecedores", "products": []}
    now = time.time()
    ck = (key, query.lower())
    cached = _search_cache.get(ck)
    if cached and (now - cached[0]) < get_settings().search_ttl:
        products = cached[1]
    else:
        session = adapter.build_session(token)
        ov = _request_timeouts().get(key)
        if ov:
            try:
                session.timeout = ov   # override por loja (lojas lentas)
            except Exception:
                pass
        products = [p.to_dict() for p in adapter.search(query, session)]
        refreshed = adapter.dump_token(session)
        if refreshed and refreshed != token:
            store.set_token(key, refreshed)   # renew-on-use (sliding session)
        _search_cache[ck] = (now, products)
    priced = [p for p in products if p.get("price") is not None]
    # "sem preco" pode ser DESLOGADO (sessao caiu) OU ESGOTADO (fora de estoque). So sinaliza
    # needsAuth se ha itens sem preco que NAO estao marcados como esgotados.
    needs_auth = (not priced) and any(
        p.get("price") is None and p.get("in_stock") is not False for p in products)
    # relevance filter: some suppliers return "featured"/loose junk when they find nothing
    shown = products
    if get_settings().search_filter:
        tokens = _query_tokens(query)
        if tokens:
            shown = [p for p in products if _is_relevant(tokens, p)]
    return {"key": key, "name": adapter.name, "ok": True,
            "count": len(shown), "rawCount": len(products),
            "needsAuth": needs_auth, "products": shown}


@compubot.get("/api/search", dependencies=[Depends(auth.require_auth)])
async def api_search(q: str, suppliers: str = ""):
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Digite ao menos 2 caracteres.")
    active = [s["key"] for s in store.list_suppliers() if _supplier_ready(s)]
    if suppliers:  # restrict to the chosen suppliers (checkboxes)
        chosen = {k.strip() for k in suppliers.split(",") if k.strip()}
        active = [k for k in active if k in chosen]
    if not active:
        return {"query": q, "suppliers": [], "note": "Nenhum fornecedor selecionado/configurado com token."}

    def _run():
        # Fan-out com DEADLINE: retorna assim que todos terminam OU quando estoura o prazo — um
        # fornecedor lento (ex.: pauta trava ~25s numa busca logada sem resultado) nao segura mais
        # o resultado dos outros. Os que nao responderam a tempo entram como falha (thread continua
        # em background e morre no request_timeout).
        deadline = max(1, get_settings().search_deadline)
        pool = ThreadPoolExecutor(max_workers=min(8, len(active)))
        futs = {pool.submit(_search_one_safe, k, q): k for k in active}
        results = []
        try:
            for f in as_completed(futs, timeout=deadline):
                results.append(f.result())
        except _FuturesTimeout:
            pass
        for f, k in futs.items():
            if not f.done():
                ad = _resolve_adapter(k)
                results.append({"key": k, "name": getattr(ad, "name", k), "ok": False,
                                "error": f"sem resposta em {deadline}s (timeout)", "products": []})
        pool.shutdown(wait=False)
        results.sort(key=lambda r: r.get("name") or r["key"])
        return results

    started = time.time()
    results = await run_in_threadpool(_run)
    return {"query": q, "suppliers": results, "tookMs": int((time.time() - started) * 1000)}


def _search_one_safe(key: str, query: str) -> dict:
    try:
        return _search_one(key, query)
    except Exception as exc:
        adapter = _resolve_adapter(key)
        return {"key": key, "name": getattr(adapter, "name", key), "ok": False,
                "error": str(exc), "products": []}


@compubot.get("/api/search/one", dependencies=[Depends(auth.require_auth)])
async def api_search_one(q: str, supplier: str):
    """Busca UM fornecedor — a UI chama um destes por fornecedor em paralelo e mostra cada
    resultado assim que chega (carregamento incremental; um lento nao segura os outros).
    Delega ao _search_one_safe, que ja faz a checagem correta de token/no-auth por fornecedor."""
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Digite ao menos 2 caracteres.")
    return await run_in_threadpool(_search_one_safe, (supplier or "").strip(), q)


# --------------------------------------------------------------------------
# quotes (orcamentos): create/save, list, open, delete + assembled PDF export
# --------------------------------------------------------------------------
@compubot.get("/api/quotes", dependencies=[Depends(auth.require_auth)])
async def api_quotes_list():
    return {"quotes": quotes.list_quotes()}


@compubot.post("/api/quotes", dependencies=[Depends(auth.require_auth)])
async def api_quotes_save(req: QuoteRequest):
    return {"quote": quotes.save_quote(req.model_dump())}


@compubot.get("/api/quotes/{qid}", dependencies=[Depends(auth.require_auth)])
async def api_quotes_get(qid: str):
    q = quotes.get_quote(qid)
    if not q:
        raise HTTPException(status_code=404, detail="Orcamento nao encontrado.")
    return q


@compubot.delete("/api/quotes/{qid}", dependencies=[Depends(auth.require_auth)])
async def api_quotes_delete(qid: str):
    quotes.delete_quote(qid)
    return {"ok": True}


@compubot.get("/api/quotes/{qid}/pdf", dependencies=[Depends(auth.require_auth)])
async def api_quotes_pdf(qid: str):
    q = quotes.get_quote(qid)
    if not q:
        raise HTTPException(status_code=404, detail="Orcamento nao encontrado.")
    data = await run_in_threadpool(pdf.render_quote_pdf, q)
    fn = f"orcamento-{q.get('number','') or qid}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fn}"'})


# --------------------------------------------------------------------------
# keep-alive: periodically ping each supplier so sessions don't expire by inactivity,
# and re-capture any refreshed cookie (sliding sessions). Extends token life a lot.
# --------------------------------------------------------------------------
def _autologin_refresh(key: str, attempts: int = 2) -> str | None:
    """Re-mint a supplier session via Camoufox (uses .env credentials). Persists the token
    ONLY if it actually authenticates (so a failed login's guest cookie isn't saved). Retries
    on transient failures (flaky SPA login); does NOT retry a CAPTCHA (won't help and can worsen
    a lockout)."""
    from app import autologin
    if not (get_settings().autologin_enabled and autologin.can_autologin(key)):
        return None
    for i in range(max(1, attempts)):
        tok = autologin.login(key)
        if tok:
            adapter = _resolve_adapter(key)
            try:
                session = adapter.build_session(tok)
                # check_auth is weak for generic sites (just a 200) — also require a search that
                # actually returns prices, so a guest cookie from a failed login isn't saved.
                if adapter and adapter.check_auth(session) and \
                        any(p.price is not None for p in adapter.search("mouse", session)):
                    store.set_token(key, tok)
                    return tok
            except Exception:
                pass
        if "captcha" in (autologin.last_error(key) or "").lower():
            break   # captcha nao passa com retry
        if i < attempts - 1:
            print(f"[autologin] {key}: tentativa {i + 1} falhou, tentando de novo…")
            time.sleep(4)
    print(f"[autologin] {key}: nao autenticou apos {attempts} tentativa(s)")
    return None


def _keepalive_overrides() -> dict[str, int]:
    """Parse KEEPALIVE_OVERRIDES ('key:min,key:min') into {key: minutes}."""
    out: dict[str, int] = {}
    for part in (get_settings().keepalive_overrides or "").split(","):
        part = part.strip()
        if ":" in part:
            k, m = part.split(":", 1)
            try:
                out[k.strip()] = int(m)
            except ValueError:
                pass
    return out


def _keepalive_interval(key: str, overrides: dict[str, int]) -> int:
    """Minutes between pings for a supplier (per-key override or the global default)."""
    return overrides.get(key, get_settings().keepalive_minutes)


_last_ping: dict[str, float] = {}


def _keepalive_quiet_keep() -> set[str]:
    """Fornecedores que continuam recebendo o ping BARATO na janela noturna (mantem a sessao
    viva) porque nao conseguem re-logar sozinhos — ex.: agis (captcha). Config KEEPALIVE_QUIET_KEEP."""
    raw = get_settings().keepalive_quiet_keep_alive or ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _keepalive_once(cheap_only: bool = False) -> None:
    """cheap_only (janela noturna): so mantem a sessao viva (check_auth + regrava cookie) dos
    fornecedores em KEEPALIVE_QUIET_KEEP; NUNCA roda o Camoufox (bootstrap/re-login) — economia."""
    overrides = _keepalive_overrides()
    keep = _keepalive_quiet_keep() if cheap_only else None
    now = time.time()
    for s in store.list_suppliers():
        if not s.get("enabled"):
            continue
        key = s["key"]
        if cheap_only and key not in keep:
            continue   # de madrugada, so os que nao se auto-recuperam (ex.: agis)
        interval = _keepalive_interval(key, overrides)
        if interval <= 0:
            continue
        if now - _last_ping.get(key, 0.0) < interval * 60:
            continue   # not due yet
        _last_ping[key] = now
        try:
            if not s.get("hasToken"):
                if not cheap_only:
                    _autologin_refresh(key)   # bootstrap first token when creds exist (no-op otherwise)
                continue
            adapter = _resolve_adapter(key)
            token = store.get_token(key)
            if not adapter or not token:
                continue
            session = adapter.build_session(token)
            if adapter.check_auth(session):
                refreshed = adapter.dump_token(session)   # capture any renewed cookie
                if refreshed and refreshed != token:
                    store.set_token(key, refreshed)
            elif not cheap_only:
                _autologin_refresh(key)                   # expired -> re-login via Camoufox (nao de madrugada)
        except Exception:
            pass


def _brazil_hour() -> int:
    """Hora atual no fuso de Brasilia (America/Sao_Paulo). Usa zoneinfo se houver a base de
    fusos (Linux tem); senao cai no offset fixo UTC-3 (Brasil nao tem horario de verao desde 2019)."""
    from datetime import datetime, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).hour
    except Exception:
        return (datetime.now(timezone.utc).hour - 3) % 24


def _keepalive_quiet_now() -> bool:
    """True se estamos na janela silenciosa (ex.: 20h-6h) — sem testar tokens (ninguem usando)."""
    s = get_settings()
    start, end = s.keepalive_quiet_start, s.keepalive_quiet_end
    if start == end:
        return False   # janela vazia = keepalive sempre ativo
    h = _brazil_hour()
    return start <= h < end if start < end else (h >= start or h < end)


def _keepalive_loop() -> None:
    # disabled only if the global default AND every override are off
    if get_settings().keepalive_minutes <= 0 and not any(v > 0 for v in _keepalive_overrides().values()):
        return
    time.sleep(20 + (os.getpid() % 40))   # stagger workers a bit
    while True:
        # na janela noturna roda so o ping BARATO (cheap_only) dos que nao se auto-recuperam;
        # o resto (e o Camoufox) fica pausado — economia. Fora dela, keepalive normal.
        _keepalive_once(cheap_only=_keepalive_quiet_now())
        time.sleep(60)   # tick every minute; each supplier pinged on its own interval


@compubot.on_event("startup")
def _start_keepalive() -> None:
    if get_settings().keepalive_minutes > 0:
        threading.Thread(target=_keepalive_loop, daemon=True, name="keepalive").start()


# Served at the domain root.
app = compubot
