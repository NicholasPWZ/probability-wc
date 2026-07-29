"""Adapter for pauta.com.br — a nopCommerce (ASP.NET Core) B2B reseller store.

Validated against the live site:
  * Auth is the `.Nop.Authentication` cookie (sliding-expiration session — renews by
    re-capturing the refreshed cookie from responses; no re-login needed while used).
  * Cookies MUST live in the jar (the www->apex 301 drops a bare Cookie header).
  * Search: GET /search?q=TERM (server-rendered).
  * Each product carries a tiered price table: rows of UF | QTD | Preço | ST.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from curl_cffi import requests

from app.config import get_settings
from app.suppliers.base import Product, SupplierAdapter, Tier

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/149.0.0.0 Safari/537.36")
_DOMAIN = "pauta.com.br"
_BASE = "https://pauta.com.br"

_BLOCK_RE = re.compile(r'<div class="product-item cardProduct">.*?(?=<div class="product-item cardProduct">|<div class="pager|</body>)', re.S)
_PID_RE = re.compile(r'data-productid="(\d+)"')
_HREF_RE = re.compile(r'<h3 class="title[^"]*">\s*<a href="([^"]+)"', re.S)
_TITLE_RE = re.compile(r'<h3 class="title[^"]*">\s*<a[^>]*title="([^"]*)"', re.S)
_IMG_RE = re.compile(r'<img[^>]+src="([^"]+)"')
_BRAND_RE = re.compile(r'product-brand">\s*<span>[^<]*</span>\s*<span>\s*([^<]+?)\s*</span>', re.S)
_SKU_RE = re.compile(r'product-code">\s*<span>[^<]*</span>\s*<span>\s*([^<]+?)\s*</span>', re.S)
_PN_RE = re.compile(r'part-number">\s*<span>[^<]*</span>\s*<span>\s*([^<]+?)\s*</span>', re.S)
_ROW_RE = re.compile(
    r'<td class="product-region-origin">\s*([^<]*?)\s*</td>\s*'
    r'<td>\s*([\d.]*)\s*</td>\s*'
    r'<td[^>]*>\s*([\d.]*,\d{2})\s*</td>\s*'
    r'<td>\s*([\d.]*,\d{2})\s*</td>', re.S)


def _money(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


class PautaAdapter(SupplierAdapter):
    key = "pauta"
    name = "Pauta"
    base_url = _BASE
    auth_help = ("No pauta.com.br logado: DevTools (F12) > Network > clique em qualquer "
                 "requisição da pauta > Request Headers > copie o valor inteiro de 'cookie' "
                 "e cole aqui.")

    def build_session(self, token: str):
        s = requests.Session(impersonate="chrome", timeout=get_settings().request_timeout)
        s.headers.update({
            "user-agent": _UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "pt-BR,pt;q=0.9,en;q=0.8",
        })
        for part in (token or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=_DOMAIN)
        return s

    def dump_token(self, session) -> str:
        # persist the current jar (captures any refreshed .Nop.Authentication).
        # curl_cffi Cookies.items() yields (name, value) pairs.
        return "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v is not None)

    def check_auth(self, session) -> bool:
        r = session.get(f"{_BASE}/customer/info", allow_redirects=False)
        loc = (r.headers.get("location") or "").lower()
        return r.status_code == 200 and "login" not in loc

    def search(self, query: str, session) -> list[Product]:
        r = session.get(f"{_BASE}/search?q={quote(query)}")
        return self._parse(r.text)

    def _parse(self, html: str) -> list[Product]:
        out: list[Product] = []
        for block in _BLOCK_RE.findall(html):
            href = (_HREF_RE.search(block) or [None, None])[1] if _HREF_RE.search(block) else None
            hm = _HREF_RE.search(block)
            href = hm.group(1) if hm else None
            if not href:
                continue
            tm = _TITLE_RE.search(block)
            name = _clean(tm.group(1)) if tm else _clean(href.strip("/").replace("-", " "))
            tiers: list[Tier] = []
            for uf, qty, price, st in _ROW_RE.findall(block):
                tiers.append(Tier(region=_clean(uf) or None,
                                  qty=int(qty) if qty.isdigit() else None,
                                  price=_money(price), extra=_money(st)))
            prices = [t.price for t in tiers if t.price is not None]
            best = min(prices) if prices else None
            stock = sum(t.qty for t in tiers if t.qty) if tiers else None
            im = _IMG_RE.search(block)
            bm, sm, pm, pid = (_BRAND_RE.search(block), _SKU_RE.search(block),
                               _PN_RE.search(block), _PID_RE.search(block))
            out.append(Product(
                name=name,
                url=href if href.startswith("http") else _BASE + href,
                price=best,
                price_text=(f"R$ {best:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                            if best is not None else None),
                image=im.group(1) if im else None,
                brand=_clean(bm.group(1)) if bm else None,
                sku=_clean(sm.group(1)) if sm else None,
                part_number=_clean(pm.group(1)) if pm else None,
                product_id=pid.group(1) if pid else None,
                stock=stock,
                in_stock=(stock or 0) > 0 if stock is not None else None,
                tiers=tiers,
            ))
        return out
