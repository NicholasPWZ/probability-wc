"""Adapter for brailedistribuidora.com.br — a WMW / UP Server SPA backed by a JSON API.

Products are NOT in the HTML (JS SPA), so this is a code adapter (the generic
selector adapter can't handle it). Search is a POST to a WMW query-by-example
endpoint; the account-specific ids in the payload (cdEmpresa/cdCliente/
cdGrupoCliente/cdUsuario/sessionId) are read from the pasted `wmw-session-50`
cookie, so nothing is hardcoded to one account.
"""
from __future__ import annotations

import base64
import json
import re
from urllib.parse import unquote

from curl_cffi import requests

from app.config import get_settings
from app.suppliers.base import Product, SupplierAdapter, Tier

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/149.0.0.0 Safari/537.36")
_BASE = "https://www.brailedistribuidora.com.br"
_API = _BASE + "/up-server/public/service/produto/findProdutoList/produtoController/findAllByExampleByPages"


def _brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if v else None


class BraileAdapter(SupplierAdapter):
    key = "braile"
    name = "Braile Distribuidora"
    base_url = _BASE
    auth_help = ("No brailedistribuidora.com.br logado: DevTools > Network > qualquer requisição > "
                 "Request Headers > copie o valor inteiro de 'cookie' (contém JSESSIONID e "
                 "wmw-session-50) e cole aqui.")

    def _params(self, token: str) -> dict:
        """Account ids from the pasted cookie (wmw-session-50 JSON + JSESSIONID)."""
        p = {"cdEmpresa": "1-1", "cdCliente": "", "cdGrupoCliente": "", "cdUsuario": "", "sessionId": ""}
        for part in (token or "").split(";"):
            part = part.strip()
            if part.startswith("JSESSIONID="):
                p["sessionId"] = part.split("=", 1)[1]
            elif part.startswith("wmw-session-50="):
                try:
                    d = json.loads(unquote(part.split("=", 1)[1]))
                    p["cdEmpresa"] = (d.get("empresa") or {}).get("cdEmpresa") or p["cdEmpresa"]
                    cli = d.get("cliente") or {}
                    p["cdCliente"] = cli.get("cdCliente") or ""
                    p["cdGrupoCliente"] = cli.get("cdGrupoCliente") or ""
                    p["cdUsuario"] = (d.get("usuario") or {}).get("cdUsuario") or ""
                    if d.get("sessionId"):
                        p["sessionId"] = d["sessionId"]
                except Exception:
                    pass
        return p

    def build_session(self, token: str):
        s = requests.Session(impersonate="chrome", timeout=get_settings().request_timeout)
        s.headers.update({
            "user-agent": _UA, "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8", "origin": _BASE, "referer": _BASE + "/",
        })
        for part in (token or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain="brailedistribuidora.com.br")
        s.braile_params = self._params(token)   # stash account ids for search()
        return s

    def dump_token(self, session) -> str:
        return "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v is not None)

    def _call(self, session, query: str, page: int = 1):
        p = getattr(session, "braile_params", None) or {}
        payload = {
            "cdEmpresa": p.get("cdEmpresa", "1-1"), "cdClienteFilter": p.get("cdCliente", ""),
            "clienteEmpresa": {}, "cdGrupoClienteFilter": p.get("cdGrupoCliente", ""),
            "dsPalavraChave": query, "pageLines": 24, "currentPage": page, "filtros": {},
            "sortColumns": "1-1-PRODUTO-1",
            "session": {"cdSistema": 50, "sessionId": p.get("sessionId", ""),
                        "isUsuarioAnomimo": False, "usuario": {"cdUsuario": p.get("cdUsuario", "")}},
            "flAtivo": "S",
        }
        r = session.post(_API, data=json.dumps(payload))
        if "json" not in (r.headers.get("content-type") or ""):
            return None
        return r.json()

    def check_auth(self, session) -> bool:
        j = self._call(session, "mouse")
        return isinstance(j, dict) and "collections" in j

    def _product_url(self, it: dict) -> str:
        cat = (it.get("produtoCategoriaList") or [{}])[0]
        tok = {"cdProduto": it.get("cdProduto"),
               "cdDepartamento": cat.get("cdDepartamento"), "cdCategoria": cat.get("cdCategoria")}
        b64 = base64.b64encode(json.dumps(tok).encode("utf-8")).decode("utf-8")
        return f"{_BASE}/produto/{b64}"

    def search(self, query: str, session) -> list[Product]:
        j = self._call(session, query)
        prods = ((j or {}).get("collections") or [[]])[0]
        out = []
        for it in prods:
            tp = it.get("itemTabelaPreco") or {}
            price = tp.get("vlPreco") or None
            stock = it.get("qtEstoque")
            name = re.sub(r"\s*\[[^\]]+\]\s*$", "", it.get("dsProduto") or it.get("cdProduto") or "").strip()
            out.append(Product(
                name=name or it.get("cdProduto"),
                url=self._product_url(it),
                price=price,
                price_text=_brl(price),
                brand=it.get("marca") or it.get("cdMarca"),
                sku=it.get("cdProduto"),
                in_stock=(stock or 0) > 0 if stock is not None else None,
                stock=int(stock) if stock else None,
                tiers=[Tier(price=price)] if price else [],
            ))
        return out
