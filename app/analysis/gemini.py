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


def _client_and_types():
    from app import runtime
    if not runtime.gemini_enabled():
        raise GeminiUnavailable("GEMINI_API_KEY is not configured.")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover
        raise GeminiUnavailable("google-genai is not installed.") from exc
    return genai.Client(api_key=runtime.gemini_api_key()), types


def _explain_api_error(exc: Exception) -> str:
    """Turn a raw google-genai error into a clear, user-facing message."""
    from app import runtime

    msg = str(exc) or exc.__class__.__name__
    low = msg.lower()
    model = runtime.gemini_model()
    if "api key not valid" in low or "api_key_invalid" in low or "permission_denied" in low:
        return ("A chave do Gemini é inválida ou não tem acesso à Generative Language API. "
                "Verifique a chave e se a API está habilitada no Google AI Studio. "
                f"(detalhe: {msg[:200]})")
    if "not found" in low or "404" in low or "is not supported" in low:
        return (f"O modelo '{model}' não existe ou não está disponível para esta chave. "
                "Use um modelo válido (ex.: gemini-2.5-flash, gemini-2.0-flash). "
                f"(detalhe: {msg[:200]})")
    if "quota" in low or "429" in low or "resource_exhausted" in low:
        return (f"Cota/limite do Gemini excedido para o modelo '{model}'. "
                f"Tente novamente mais tarde. (detalhe: {msg[:200]})")
    return f"Falha ao chamar o Gemini (modelo '{model}'): {msg[:300]}"


def _run(make_response) -> dict:
    try:
        response = make_response()
    except GeminiUnavailable:
        raise
    except Exception as exc:  # google.genai.errors.APIError and friends
        raise GeminiUnavailable(_explain_api_error(exc)) from exc

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        raise GeminiUnavailable("O Gemini retornou uma resposta vazia (possível bloqueio de conteúdo ou modelo inválido).")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiUnavailable(f"Gemini retornou saída não-JSON: {text[:200]}") from exc


def analyze_with_gemini(dataset: dict, engine_output: dict, reliability: dict | None = None) -> dict:
    from app import runtime
    client, types = _client_and_types()
    contents = prompt_mod.build_contents(dataset, engine_output, reliability)
    return _run(lambda: client.models.generate_content(
        model=runtime.gemini_model(),
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=prompt_mod.SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=prompt_mod.RESPONSE_SCHEMA,
            temperature=0.3,
        ),
    ))


def synthesize_with_gemini(analysis1: dict, analysis2: dict) -> dict:
    """Final consensus: compare two prior analyses, keep only what both agree on."""
    from app import runtime
    client, types = _client_and_types()
    return _run(lambda: client.models.generate_content(
        model=runtime.gemini_model(),
        contents=prompt_mod.build_synthesis_contents(analysis1, analysis2),
        config=types.GenerateContentConfig(
            system_instruction=prompt_mod.SYNTHESIS_SYSTEM,
            response_mime_type="application/json",
            response_schema=prompt_mod.SYNTHESIS_SCHEMA,
            temperature=0.2,
        ),
    ))
