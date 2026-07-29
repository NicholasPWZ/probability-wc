"""Mock supplier — lets the whole search/compare flow work without any real token.
Handy for developing the UI and verifying the framework end-to-end."""
from __future__ import annotations

import hashlib

from app.suppliers.base import Product, SupplierAdapter, Tier


class MockAdapter(SupplierAdapter):
    key = "mock"
    name = "Fornecedor Demo"
    base_url = "https://example.com"
    auth_help = "Cole qualquer texto como token (o mock aceita tudo, só p/ testar)."

    def build_session(self, token: str):
        return {"token": token}

    def dump_token(self, session) -> str:
        return session.get("token", "") if isinstance(session, dict) else ""

    def check_auth(self, session) -> bool:
        return bool(isinstance(session, dict) and session.get("token"))

    def search(self, query: str, session) -> list[Product]:
        out = []
        for i in range(1, 4):
            seed = int(hashlib.sha256(f"{query}{i}".encode()).hexdigest(), 16)
            price = round(20 + (seed % 8000) / 100, 2)
            out.append(Product(
                name=f"{query.title()} — variação {i}",
                url=f"https://example.com/p/{seed % 100000}",
                price=price, price_text=f"R$ {price:.2f}".replace(".", ","),
                brand="DemoBrand", sku=str(seed % 100000),
                in_stock=(seed % 3 != 0), stock=(seed % 50),
                tiers=[Tier(region="SP", qty=seed % 50, price=price)],
            ))
        return out


class MockAdapterB(MockAdapter):
    key = "mock2"
    name = "Fornecedor Demo 2"
