"""Request models for the API."""
from __future__ import annotations

from pydantic import BaseModel


class AnalyzeUrlRequest(BaseModel):
    url: str
