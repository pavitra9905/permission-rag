import logging
from functools import lru_cache

import anthropic
import httpx

from app.core.config import settings

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """No key / server not running, or the call failed."""


@lru_cache(maxsize=1)
def _anthropic_client() -> anthropic.Anthropic:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=key, timeout=settings.llm_timeout_seconds, max_retries=2)


def _claude(system: str, user: str) -> str:
    message = _anthropic_client().messages.create(
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in message.content if b.type == "text")


def _ollama(system: str, user: str) -> str:
    resp = httpx.post(
        f"{settings.ollama_base_url}/api/chat",
        json={
            "model": settings.ollama_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"num_predict": settings.llm_max_tokens, "num_ctx": 4096},
        },
        timeout=settings.llm_timeout_seconds,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _gemini(system: str, user: str) -> str:
    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    if not key:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
        headers={"x-goog-api-key": key},  # header, not URL, so it never lands in logs
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": settings.llm_max_tokens},
        },
        timeout=settings.llm_timeout_seconds,
    )
    resp.raise_for_status()
    parts = resp.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts)


_PROVIDERS = {"anthropic": _claude, "ollama": _ollama, "gemini": _gemini}


def generate_answer(*, system: str, user: str) -> str:
    """The ONLY place the app talks to an LLM. Never logs prompts (they hold private text)."""
    try:
        return _PROVIDERS[settings.llm_provider](system, user).strip()
    except LLMUnavailable:
        raise
    except (anthropic.APIError, httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        log.error("LLM call failed via %s: %s", settings.llm_provider, type(exc).__name__)
        raise LLMUnavailable("LLM request failed") from exc