"""Request models for the API."""
from __future__ import annotations

from pydantic import BaseModel


class AnalyzeUrlRequest(BaseModel):
    url: str


class GeminiSettingsRequest(BaseModel):
    token: str
    apiKey: str | None = None
    model: str | None = None


class GeminiRunRequest(BaseModel):
    action: str | None = None   # "new_section" (admin-gated) or None = progress active section
    token: str | None = None    # admin token, required when action == "new_section"
