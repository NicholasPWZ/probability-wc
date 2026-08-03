"""Adapter for vendas.agis.com.br - a Magento B2B store (DigitalHub/Agis theme).

Products come from the server-rendered catalog search, but PRICES are loaded by JS
(module `Agis_Pricing/js/listing-price.js`) via a separate POST once logged in -> a
generic selector adapter can't read them, so this is a code adapter (like braile).

Flow: (1) GET catalogsearch/result/?q= to get product items (name/url/image/SKU), then
(2) POST the page SKUs to `/integration/listing/price` to get the per-warehouse prices.
Auth = the logged-in cookie (Magento PHPSESSID etc.); the price endpoint 401s/empties for
guests. Prices are per WAREHOUSE (`data-warehouse`), so multiple warehouses become tiers.
"""
from __future__ import annotations

from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi import requests

from app.config import get_settings
from app.suppliers.base import Product, SupplierAdapter, Tier

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/149.0.0.0 Safari/537.36")
_BASE = "https://vendas.agis.com.br"
_SEARCH = _BASE + "/catalogsearch/result/?q="
_PRICE_API = _BASE + "/integration/listing/price"


def _brl(v):
    if v is None:
        return None
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class AgisAdapter(SupplierAdapter):
    key = "agis"
    name = "Agis"
    base_url = _BASE
    auth_help = ("No vendas.agis.com.br logado: DevTools > Network > qualquer requisicao > "
                 "Request Headers > copie o valor inteiro de 'cookie' e cole aqui. (Ou configure "
                 "AGIS_USER/AGIS_PASS no .env para o auto-login.)")

    def build_session(self, token: str):
        s = requests.Session(impersonate="chrome", timeout=get_settings().request_timeout)
        s.headers.update({"user-agent": _UA,
                          "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                          "accept-language": "pt-BR,pt;q=0.9,en;q=0.8"})
        for part in (token or "").split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain="vendas.agis.com.br")
        return s

    def dump_token(self, session) -> str:
        return "; ".join(f"{k}={v}" for k, v in session.cookies.items() if v is not None)

    def check_auth(self, session) -> bool:
        """Logged out -> /customer/account/ redirects to /customer/account/login."""
        try:
            r = session.get(_BASE + "/customer/account/")
            return "/customer/account/login" not in (r.url or "")
        except Exception:
            return False

    def _prices(self, session, skus: list[str]) -> dict[str, list[tuple[str, float]]]:
        """POST the SKUs -> {sku: [(warehouse, price), ...]} (only priced/available ones)."""
        if not skus:
            return {}
        r = session.post(_PRICE_API, data=[("productSkus[]", sk) for sk in skus],
                         headers={"X-Requested-With": "XMLHttpRequest",
                                  "accept": "application/json, text/javascript, */*; q=0.01",
                                  "origin": _BASE, "referer": _BASE + "/catalogsearch/result/"})
        if "json" not in (r.headers.get("content-type") or ""):
            return {}
        try:
            data = r.json()
        except Exception:
            return {}
        out: dict[str, list[tuple[str, float]]] = {}
        if not isinstance(data, dict):
            return {}
        for wh, plist in data.items():          # top level = warehouse code
            if not isinstance(plist, dict):
                continue
            for sku, d in plist.items():
                if not isinstance(d, dict):
                    continue
                p = d.get("price")
                if p in (None, "", 0):
                    continue
                try:
                    out.setdefault(sku, []).append((str(d.get("warehouse") or wh), round(float(p), 2)))
                except (TypeError, ValueError):
                    pass
        return out

    def search(self, query: str, session) -> list[Product]:
        pages = max(1, get_settings().search_pages)
        parsed = []            # (name, url, image, sku, unavailable)
        skus: list[str] = []
        seen_sku: set = set()
        final = None
        for page in range(1, pages + 1):
            if page == 1:
                r = session.get(_SEARCH + quote(query))
                final = str(r.url)   # a busca redireciona p/ a categoria; paginar na URL FINAL
            else:
                r = session.get(final + ("&" if "?" in final else "?") + f"p={page}")
            soup = BeautifulSoup(r.text, "html.parser")
            page_added = 0
            for it in soup.select(".product-item"):
                link = it.select_one("a.product-item-link")
                if not link:
                    continue
                name = link.get_text(strip=True)
                url = link.get("href") or _BASE
                img_el = it.select_one("img.product-image-photo") or it.find("img")
                image = (img_el.get("src") or img_el.get("data-src")) if img_el else None
                wp = it.select_one(".warehouse-price[data-product-sku]")
                sku = wp.get("data-product-sku") if wp else None
                if sku and sku in seen_sku:
                    continue   # dedup entre paginas
                if sku:
                    seen_sku.add(sku)
                # marcador EXPLICITO de esgotado (nao confundir com "sem preco por estar deslogado")
                unavailable = "indispon" in (wp.get_text(" ").lower() if wp else "")
                parsed.append((name, url, image, sku, unavailable))
                page_added += 1
                if sku and sku not in skus:
                    skus.append(sku)
            if page_added == 0:
                break

        prices = self._prices(session, skus)
        out = []
        for name, url, image, sku, unavailable in parsed:
            tiers_raw = sorted(prices.get(sku or "", []), key=lambda x: x[1])
            price = tiers_raw[0][1] if tiers_raw else None
            multi = len(tiers_raw) > 1
            tiers = [Tier(price=pp, region=(f"Dep {wh}" if multi else None)) for wh, pp in tiers_raw] \
                if price is not None else []
            # com preco = disponivel; "Indisponivel" explicito = esgotado; senao desconhecido (None)
            in_stock = True if price is not None else (False if unavailable else None)
            out.append(Product(
                name=name or sku or "(sem nome)",
                url=url,
                price=price,
                price_text=_brl(price),
                image=image,
                sku=sku,
                in_stock=in_stock,
                tiers=tiers,
            ))
        return out
