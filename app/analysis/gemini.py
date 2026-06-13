"""Optional Gemini analysis layer.

Only invoked by the "Let Gemini analyze this match" endpoint. The app and its
statistical engine work fully without an API key.
"""
from __future__ import annotations

import json

from app.analysis import prompt as prompt_mod
from app.config import get_settings


class GeminiUnavailable(RuntimeError):
    pass


def analyze_with_gemini(dataset: dict, engine_output: dict) -> dict:
    settings = get_settings()
    if not settings.gemini_enabled:
        raise GeminiUnavailable("GEMINI_API_KEY is not configured.")

    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise GeminiUnavailable("google-genai is not installed.") from exc

    client = genai.Client(api_key=settings.gemini_api_key)
    contents = prompt_mod.build_contents(dataset, engine_output)

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=prompt_mod.SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=prompt_mod.RESPONSE_SCHEMA,
            temperature=0.4,
        ),
    )

    text = (response.text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiUnavailable(f"Gemini returned non-JSON output: {text[:200]}") from exc
