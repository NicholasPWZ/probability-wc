"""Supplier adapter registry — import each adapter here to register it."""
from app.suppliers.base import Product, SupplierAdapter, all_adapters, get_adapter, register
from app.suppliers.agis import AgisAdapter
from app.suppliers.braile import BraileAdapter
from app.suppliers.mock import MockAdapter, MockAdapterB
from app.suppliers.pauta import PautaAdapter

register(PautaAdapter())
register(BraileAdapter())
register(AgisAdapter())
register(MockAdapter())
register(MockAdapterB())

# Known suppliers seeded on a fresh install (NO tokens — pasted per environment). Lets a
# deploy list all suppliers ready-for-token instead of re-typing selector configs by hand.
SEED = [
    {"key": "pauta", "kind": "builtin", "name": "Pauta", "baseUrl": "https://pauta.com.br"},
    {"key": "braile", "kind": "builtin", "name": "Braile Distribuidora",
     "baseUrl": "https://www.brailedistribuidora.com.br"},
    {"key": "mazer", "kind": "generic", "name": "Mazer", "baseUrl": "https://www.mazer.com.br",
     "config": {"searchUrl": "https://www.mazer.com.br/busca?s={q}", "authMode": "cookie", "pageParam": "pagina",
                "priceLocale": "br", "linkAttr": "href", "imageAttr": "src",
                "selectors": {"item": "li:has(.nome-produto-3linhas)", "name": ".nome-produto-3linhas",
                              "price": "ins.novo-valor", "link": ".box-img-listagem a",
                              "image": ".box-img-listagem img", "stock": ""}}},
    {"key": "digimacro", "kind": "generic", "name": "Digimacro", "baseUrl": "https://digimacro.com.br",
     "config": {"searchUrl": "https://digimacro.com.br/pesquisa?controller=search&s={q}",
                "authMode": "cookie", "pageParam": "page", "priceLocale": "br", "linkAttr": "href", "imageAttr": "content",
                "selectors": {"item": "article.product-miniature", "name": ".product-title",
                              "price": ".price", "link": ".product-title a",
                              "image": ".product-thumbnail img", "stock": ""}}},
    {"key": "multimarcas", "kind": "generic", "name": "Multimarcas Distribuidora",
     "baseUrl": "https://www.multimarcasdistribuidora.com.br",
     "config": {"searchUrl": "https://www.multimarcasdistribuidora.com.br/loja/busca.php?loja=1414368&palavra_busca={q}",
                "authMode": "cookie", "pageParam": "pg", "priceLocale": "br", "linkAttr": "href", "imageAttr": "data-src",
                "selectors": {"item": ".product", "name": ".product-name", "price": ".current-price",
                              "link": ".product-info", "image": ".image img", "stock": ""}}},
    # Tray como o multimarcas (loja=571937). Preco publico (sem login) -> authMode none, sem token.
    # Imagem ja vem no src (nao e placeholder tcdn).
    {"key": "gaucha", "kind": "generic", "name": "Gaucha Distribuidora",
     "baseUrl": "https://www.gauchadistribuidora.com.br",
     "config": {"searchUrl": "https://www.gauchadistribuidora.com.br/loja/busca.php?loja=571937&palavra_busca={q}",
                "authMode": "none", "pageParam": "pg", "priceLocale": "br", "linkAttr": "href", "imageAttr": "src",
                "selectors": {"item": ".product", "name": ".product-name", "price": ".current-price",
                              "link": ".product-info", "image": ".image img", "stock": ""}}},
    # Mercadao da Informatica (Loja Integrada). Preco PUBLICO (sem login) -> authMode none. Card da
    # listagem e li.span4; preco em .preco-promocional; imagem real no src (CDN awsli).
    {"key": "mercadao", "kind": "generic", "name": "Mercadao da Informatica",
     "baseUrl": "https://www.mercadaodainformatica.com.br",
     "config": {"searchUrl": "https://www.mercadaodainformatica.com.br/buscar?q={q}",
                "authMode": "none", "pageParam": "pagina", "priceLocale": "br", "linkAttr": "href", "imageAttr": "src",
                "selectors": {"item": "li.span4", "name": ".nome-produto", "price": ".preco-promocional",
                              "link": "a", "image": "img", "stock": ""}}},
    # RE Atacado (OpenCart). Loja de ATACADO: preco so aparece LOGADO -> authMode cookie (revendedor
    # cola o cookie). Busca server-rendered: index.php?route=product/search&search={q}; cards em
    # .product-thumb; preco em .price (o novo vem primeiro, entao parse_price pega o preco de venda);
    # paginacao por &page=N. Imagem real no src (image/cache/...).
    {"key": "reatacado", "kind": "generic", "name": "RE Atacado",
     "baseUrl": "https://www.reatacado.com.br",
     "config": {"searchUrl": "https://www.reatacado.com.br/index.php?route=product/search&search={q}",
                "authMode": "cookie", "pageParam": "page", "priceLocale": "br", "linkAttr": "href", "imageAttr": "src",
                "selectors": {"item": ".product-thumb", "name": ".caption h4 a", "price": ".price",
                              "link": ".caption h4 a", "image": ".image img", "stock": ""}}},
    # Agis (Magento B2B). PRECO carregado por JS (POST /integration/listing/price) -> adapter em
    # CODIGO (app/suppliers/agis.py), nao generic. Auth por cookie + auto-login (receita em autologin.py).
    {"key": "agis", "kind": "builtin", "name": "Agis", "baseUrl": "https://vendas.agis.com.br"},
]

from app import store as _store
_store.ensure_seed(SEED)

__all__ = ["Product", "SupplierAdapter", "all_adapters", "get_adapter", "register", "SEED"]
