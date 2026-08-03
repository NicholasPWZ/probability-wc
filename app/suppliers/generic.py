"""User-configurable supplier adapter (no code needed).

The user defines a site from the UI: search-URL template, auth mode, and CSS
selectors for the product container + name/price/link/image/stock. This adapter
scrapes any *server-rendered* HTML site from that config. (JS-rendered sites whose
products load via AJAX won't work here — those still need a hand-written adapter.)
"""
from __future__ import annotations

import re
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from app.config import get_settings
from app.suppliers.base import Product, SupplierAdapter, Tier

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/149.0.0.0 Safari/537.36")

# marcadores de "fora de estoque" no texto do item (sites genericos raramente tem seletor de estoque,
# mas quase sempre trocam o botao "comprar" por "esgotado"/"avise-me")
_OOS_MARKERS = ("esgotado", "esgotada", "indispon", "avise", "fora de estoque", "sem estoque")


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def parse_price(text: str, locale: str = "br") -> float | None:
    if not text:
        return None
    m = re.search(r"\d[\d.,]*", text)
    if not m:
        return None
    s = m.group(0)
    if locale == "br":              # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    else:                           # us: 1,234.56 -> 1234.56
        s = s.replace(",", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _price_text(v: float | None, locale: str) -> str | None:
    if v is None:
        return None
    if locale == "br":
        return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"$ {v:,.2f}"


class GenericAdapter(SupplierAdapter):
    """Instantiated per stored generic-site config (not registered globally)."""

    auth_help = ("Cole o header de autenticação do site logado (DevTools > Network > Request "
                 "Headers): o 'cookie' inteiro (modo cookie) ou 'Nome: valor' (modo header).")

    def __init__(self, key: str, name: str, base_url: str, config: dict):
        self.key = key
        self.name = name or key
        self.base_url = (base_url or "").rstrip("/")
        self.config = config or {}
        self.selectors = self.config.get("selectors") or {}
        self.auth_mode = self.config.get("authMode", "cookie")
        self.locale = self.config.get("priceLocale", "br")
        self.link_attr = self.config.get("linkAttr", "href")
        self.image_attr = self.config.get("imageAttr", "src")
        self.search_url = self.config.get("searchUrl", "")

    def _host(self) -> str:
        host = urlparse(self.base_url).netloc
        return host[4:] if host.startswith("www.") else host

    def build_session(self, token: str):
        s = requests.Session(impersonate="chrome", timeout=get_settings().request_timeout)
        s.headers.update({"user-agent": _UA,
                          "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                          "accept-language": "pt-BR,pt;q=0.9,en;q=0.8"})
        token = (token or "").strip()
        if not token:
            return s
        if self.auth_mode == "cookie":
            domain = self._host()
            for part in token.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    s.cookies.set(k.strip(), v.strip(), domain=domain)
        elif self.auth_mode == "header":
            for line in token.splitlines():
                if ":" in line:
                    hk, hv = line.split(":", 1)
                    s.headers[hk.strip()] = hv.strip()
        return s

    def dump_token(self, session) -> str:
        if self.auth_mode == "cookie":
            return "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v is not None)
        return ""   # header/none: nothing to renew from the jar

    def check_auth(self, session) -> bool:
        try:
            r = session.get(self.base_url or self.search_url.split("{q}")[0])
            return r.status_code == 200
        except Exception:
            return False

    def search(self, query: str, session) -> list[Product]:
        if "{q}" not in self.search_url:
            raise ValueError("searchUrl precisa conter {q}")
        base = self.search_url.replace("{q}", quote(query))
        param = (self.config.get("pageParam") or "").strip()
        pages = max(1, get_settings().search_pages)
        out: list[Product] = []
        seen: set[str] = set()
        for page in range(1, pages + 1):
            url = base if (page == 1 or not param) else base + ("&" if "?" in base else "?") + f"{param}={page}"
            new = [p for p in self.parse(session.get(url).text) if (p.url or p.name) not in seen]
            for p in new:
                seen.add(p.url or p.name)
            out.extend(new)
            if not param or not new:   # sem paginacao configurada, ou pagina sem novidade -> para
                break
        return out

    def parse(self, html: str) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        sel = self.selectors
        item_sel = sel.get("item")
        items = soup.select(item_sel) if item_sel else []
        out: list[Product] = []
        for it in items[:60]:
            name = _txt(it.select_one(sel["name"])) if sel.get("name") else _txt(it)[:120]
            price = parse_price(_txt(it.select_one(sel["price"])) if sel.get("price") else "", self.locale)
            link_el = it.select_one(sel["link"]) if sel.get("link") else it.find("a")
            href = (link_el.get(self.link_attr) if link_el else None) or ""
            img_el = it.select_one(sel["image"]) if sel.get("image") else it.find("img")
            img = (img_el.get(self.image_attr) or img_el.get("src") or img_el.get("data-src")) if img_el else None
            stock_txt = _txt(it.select_one(sel["stock"])) if sel.get("stock") else ""
            if not name and not price:
                continue
            # fora de estoque: seletor de estoque (se configurado) OU marcador no texto do item
            oos = any(m in _txt(it).lower() for m in _OOS_MARKERS)
            in_stock = (False if oos
                        else None if not stock_txt else ("indispon" not in stock_txt.lower()))
            out.append(Product(
                name=name or "(sem nome)",
                url=urljoin(self.base_url + "/", href) if href else self.base_url,
                price=price,
                price_text=_price_text(price, self.locale),
                image=urljoin(self.base_url + "/", img) if img else None,
                in_stock=in_stock,
                tiers=[Tier(price=price)] if price is not None else [],
            ))
        return out
