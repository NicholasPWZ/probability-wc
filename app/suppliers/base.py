"""Supplier adapter interface + registry.

Each supplier site is a small adapter that knows how to (a) build an authenticated
HTTP session from a stored token, (b) search by free text and parse products/prices,
and (c) check whether the session is still logged in. Sessions carry the token in a
cookie jar (so redirects don't drop it); after each use the framework re-reads the jar
and persists any refreshed token — that's the "renew without re-login" for sliding
cookie sessions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Tier:
    region: str | None = None       # UF / warehouse
    qty: int | None = None          # available stock
    price: float | None = None      # unit price
    extra: float | None = None      # e.g. tax (ST)


@dataclass
class Product:
    name: str
    url: str
    price: float | None = None      # headline (best/lowest) price
    price_text: str | None = None
    currency: str = "BRL"
    image: str | None = None
    brand: str | None = None
    sku: str | None = None
    part_number: str | None = None
    product_id: str | None = None
    in_stock: bool | None = None
    stock: int | None = None
    tiers: list[Tier] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tiers"] = [asdict(t) for t in self.tiers]
        return d


class SupplierAdapter:
    key: str = ""
    name: str = ""
    base_url: str = ""
    auth_help: str = "Cole o token/cookie de autenticação copiado do navegador (logado)."

    def build_session(self, token: str):
        """Return an authenticated curl_cffi Session from the stored token."""
        raise NotImplementedError

    def dump_token(self, session) -> str:
        """Serialize the (possibly refreshed) session back to a storable token string."""
        raise NotImplementedError

    def check_auth(self, session) -> bool:
        """True if the session is still logged in."""
        raise NotImplementedError

    def search(self, query: str, session) -> list[Product]:
        raise NotImplementedError


_REGISTRY: dict[str, SupplierAdapter] = {}


def register(adapter: SupplierAdapter) -> None:
    _REGISTRY[adapter.key] = adapter


def get_adapter(key: str) -> SupplierAdapter | None:
    return _REGISTRY.get(key)


def all_adapters() -> list[SupplierAdapter]:
    return list(_REGISTRY.values())
