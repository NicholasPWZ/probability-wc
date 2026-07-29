"""Request models for the API."""
from __future__ import annotations

from pydantic import BaseModel


class LoginRequest(BaseModel):
    password: str


class SupplierUpsert(BaseModel):
    key: str                      # adapter key (e.g. "pauta")
    name: str | None = None
    baseUrl: str | None = None
    enabled: bool | None = None
    note: str | None = None


class TokenRequest(BaseModel):
    token: str


class SiteConfig(BaseModel):
    """User-defined generic site (configured from the UI, no code)."""
    key: str
    name: str
    baseUrl: str
    searchUrl: str                 # must contain {q}
    authMode: str = "cookie"       # cookie | header | none
    priceLocale: str = "br"        # br | us
    selectors: dict = {}           # {item, name, price, link, image, stock}
    linkAttr: str = "href"
    imageAttr: str = "src"
    token: str | None = None       # optional; set/replace the auth token


class SiteTest(SiteConfig):
    query: str = "teste"           # sample term for a dry-run parse
