"""Single entry point for LLM calls: complete(system, user) -> str.

If no provider is configured, or the call fails for any reason, LLMUnavailable is raised
and the caller MUST fall back to deterministic rule-based logic.

Supported providers (LLM_PROVIDER): "anthropic" (needs `pip install anthropic`).
"""
from __future__ import annotations

from .config import get_settings


class LLMUnavailable(RuntimeError):
    pass


def is_available() -> bool:
    return get_settings().llm_provider == "anthropic"


def complete(system: str, user: str, max_tokens: int = 16000) -> str:
    settings = get_settings()
    if not settings.llm_provider:
        raise LLMUnavailable("LLM_PROVIDER is not set")
    if settings.llm_provider != "anthropic":
        raise LLMUnavailable(f"unsupported LLM_PROVIDER {settings.llm_provider!r}")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMUnavailable("the 'anthropic' package is not installed") from exc

    try:
        client = anthropic.Anthropic(api_key=settings.llm_api_key or None, timeout=120.0)
        response = client.beta.messages.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as exc:  # any SDK, auth or network error means: use the rule-based path
        raise LLMUnavailable(f"LLM call failed: {exc}") from exc

    if response.stop_reason == "refusal":
        raise LLMUnavailable("LLM declined the request")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise LLMUnavailable("LLM returned no text")
    return text
