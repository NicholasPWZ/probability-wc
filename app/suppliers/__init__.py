"""Supplier adapter registry — import each adapter here to register it."""
from app.suppliers.base import Product, SupplierAdapter, all_adapters, get_adapter, register
from app.suppliers.braile import BraileAdapter
from app.suppliers.mock import MockAdapter, MockAdapterB
from app.suppliers.pauta import PautaAdapter

register(PautaAdapter())
register(BraileAdapter())
register(MockAdapter())
register(MockAdapterB())

__all__ = ["Product", "SupplierAdapter", "all_adapters", "get_adapter", "register"]
