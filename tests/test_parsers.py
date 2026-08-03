"""Regression tests for the supplier parsers.

Two kinds:
  * FIXTURE tests — run each parser against a saved real page (gzipped in tests/fixtures/).
    Catch code regressions in the parsing/selectors. Fixtures are GUEST pages (no login), so
    logged-in-only prices aren't asserted where the site hides them.
  * UNIT tests — pure functions that already bit us (braile product/catalog URL, price parsing).

Run: ./.venv/Scripts/python.exe -m pytest -q
"""
import gzip
import os
import pathlib

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return gzip.decompress((FIX / name).read_bytes()).decode("utf-8")


def _seed_config(key: str) -> dict:
    from app import suppliers
    row = next(s for s in suppliers.SEED if s["key"] == key)
    return row["config"]


# ------------------------- FIXTURE TESTS -------------------------
def test_gaucha_generic_parser():
    """Gaucha (Tray, public prices) via the generic selector engine → priced products."""
    from app.suppliers.generic import GenericAdapter
    cfg = _seed_config("gaucha")
    a = GenericAdapter("gaucha", "Gaucha", "https://www.gauchadistribuidora.com.br", cfg)
    prods = a.parse(_load("gaucha_busca.html.gz"))
    assert len(prods) >= 20, f"esperava >=20 produtos, veio {len(prods)}"
    priced = [p for p in prods if p.price is not None]
    assert len(priced) >= len(prods) * 0.8, "maioria dos produtos deve ter preco (gaucha e publico)"
    p = priced[0]
    assert p.name and p.url and p.price > 0


def test_mercadao_generic_parser():
    """Mercadao (Loja Integrada, precos publicos) via o motor generico → produtos com preco."""
    from app.suppliers.generic import GenericAdapter
    cfg = _seed_config("mercadao")
    a = GenericAdapter("mercadao", "Mercadao", "https://www.mercadaodainformatica.com.br", cfg)
    prods = a.parse(_load("mercadao_busca.html.gz"))
    assert len(prods) >= 20, f"esperava >=20 produtos, veio {len(prods)}"
    priced = [p for p in prods if p.price is not None]
    assert len(priced) >= len(prods) * 0.8, "maioria deve ter preco (mercadao e publico)"
    assert priced[0].name and priced[0].url and priced[0].price > 0


def test_pauta_parser_structure():
    """Pauta (nopCommerce) product blocks parse (guest page has no prices, but blocks/titles must parse)."""
    from app.suppliers.pauta import PautaAdapter
    prods = PautaAdapter()._parse(_load("pauta_busca.html.gz"))
    assert len(prods) >= 15, f"esperava >=15 blocos de produto, veio {len(prods)}"
    assert all(p.name for p in prods), "todo produto deve ter nome"
    assert all(p.url and p.url.startswith("http") for p in prods), "todo produto deve ter url absoluta"


def test_agis_selectors_present():
    """Agis (Magento) — os seletores de item/nome/link/imagem ainda batem na estrutura da pagina.
    (SKU/preco ficam no `.warehouse-price`, que so aparece LOGADO — nao da p/ testar no fixture guest.)"""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_load("agis_busca.html.gz"), "html.parser")
    items = soup.select(".product-item")
    assert len(items) >= 8, f"esperava >=8 .product-item, veio {len(items)}"
    assert all(it.select_one("a.product-item-link") for it in items), "todo item deve ter a.product-item-link"
    assert sum(bool(it.select_one("img.product-image-photo")) for it in items) >= len(items) * 0.8, \
        "maioria dos itens deve ter img.product-image-photo"


# ------------------------- UNIT TESTS -------------------------
def test_braile_product_url_is_encoded_catalog_search():
    """A URL de 'abrir' da braile: busca no catalogo (/produtos), base64 URL-encodado (o '=' quebrava a rota)."""
    from app.suppliers.braile import BraileAdapter
    url = BraileAdapter()._product_url({"cdProduto": "108-00400"})
    assert url.startswith("https://www.brailedistribuidora.com.br/produtos/")
    tail = url.rsplit("/", 1)[1]
    assert "=" not in tail, "o base64 tem que estar URL-encodado (%3D), nao '=' cru"
    assert "%" in tail, "esperava caracteres URL-encodados no token"


def test_braile_as_text_handles_dict_and_scalar():
    from app.suppliers.braile import _as_text
    assert _as_text({"dsMarca": "OEX"}) == "OEX"      # marca como objeto
    assert _as_text("Logitech") == "Logitech"
    assert _as_text(None) == ""
    assert _as_text({"foo": "bar"}) == ""             # sem campo de nome conhecido


def test_generic_parse_price_locales():
    from app.suppliers.generic import parse_price
    assert parse_price("R$ 1.234,56", "br") == 1234.56
    assert parse_price("$ 1,234.56", "us") == 1234.56
    assert parse_price("R$ 7,90", "br") == 7.90
    assert parse_price("sob consulta", "br") is None


def test_pauta_money_and_braile_brl():
    from app.suppliers.pauta import _money
    from app.suppliers.braile import _brl
    assert _money("1.234,56") == 1234.56
    assert _money("42,59") == 42.59
    assert _money("") is None
    assert _brl(64.2238) == "R$ 64,22"
    assert _brl(None) is None


# ------------------------- LIVE SMOKE (opcional) -------------------------
# Pega mudanca REAL no site (layout mudou -> parser quebra). Nao roda por padrao para os
# testes ficarem deterministicos/offline. Rode com: RUN_LIVE=1 pytest -q -k live
@pytest.mark.skipif(not os.environ.get("RUN_LIVE"), reason="define RUN_LIVE=1 para bater no site real")
def test_live_gaucha_still_parses():
    """Gaucha e publico (preco sem login): confirma que a busca real ainda devolve produtos com preco."""
    from app.suppliers.generic import GenericAdapter
    cfg = _seed_config("gaucha")
    a = GenericAdapter("gaucha", "Gaucha", "https://www.gauchadistribuidora.com.br", cfg)
    prods = a.search("mouse", a.build_session(""))
    priced = [p for p in prods if p.price is not None]
    assert len(priced) >= 5, f"gaucha ao vivo trouxe {len(priced)} com preco — layout pode ter mudado"
