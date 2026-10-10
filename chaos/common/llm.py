"""Single entry point for LLM calls: complete(system, user) -> str.

If no provider is configured, or the call fails for any reason, LLMUnavailable is raised
and the caller MUST fall back to deterministic rule-based logic.

Supported providers (LLM_PROVIDER):
  - "anthropic"          cloud API (needs `pip install anthropic` and LLM_API_KEY)
  - "llamacpp" / "local" a local GGUF model via llama-cpp-python, no network/API key

Local provider configuration (environment variables):
  - LLM_MODEL_PATH   absolute path to the .gguf file (falls back to LLM_MODEL if that is a path)
  - LLM_N_CTX        context window, default 8192
  - LLM_N_THREADS    CPU threads, default = all cores
  - LLM_N_GPU_LAYERS layers to offload to GPU, default 0 (CPU only)
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .config import get_settings

_LOCAL_PROVIDERS = {"llamacpp", "local"}

# DeepSeek-R1 and other reasoning models emit "<think> ... </think>" before the
# real answer. Strip the first such block so callers get the answer, not the scratchpad.
_THINK_RE = re.compile(r"^\s*<think>.*?</think>", re.DOTALL)

# The loaded local model is cached across calls (loading an 8B GGUF takes ~10-20s).
_local_llm = None
_local_llm_path: str | None = None


class LLMUnavailable(RuntimeError):
    pass


def is_available() -> bool:
    provider = get_settings().llm_provider
    return provider == "anthropic" or provider in _LOCAL_PROVIDERS


def complete(system: str, user: str, max_tokens: int = 16000) -> str:
    settings = get_settings()
    if not settings.llm_provider:
        raise LLMUnavailable("LLM_PROVIDER is not set")
    if settings.llm_provider in _LOCAL_PROVIDERS:
        return _complete_local(system, user, max_tokens)
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


# ---------------------------------------------------------------- local GGUF (llama-cpp-python)

def _model_path() -> str:
    path = os.environ.get("LLM_MODEL_PATH") or get_settings().llm_model
    if not path or not Path(path).is_file():
        raise LLMUnavailable(
            f"local model file not found: {path!r}. Set LLM_MODEL_PATH to the .gguf file."
        )
    return path


def _get_local_llm():
    global _local_llm, _local_llm_path
    path = _model_path()
    if _local_llm is not None and _local_llm_path == path:
        return _local_llm
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise LLMUnavailable("the 'llama-cpp-python' package is not installed") from exc

    threads = os.environ.get("LLM_N_THREADS")
    try:
        _local_llm = Llama(
            model_path=path,
            n_ctx=int(os.environ.get("LLM_N_CTX", "8192")),
            n_threads=int(threads) if threads else None,
            n_gpu_layers=int(os.environ.get("LLM_N_GPU_LAYERS", "0")),
            verbose=False,
        )
    except Exception as exc:
        raise LLMUnavailable(f"failed to load local model {path!r}: {exc}") from exc
    _local_llm_path = path
    return _local_llm


def _complete_local(system: str, user: str, max_tokens: int) -> str:
    llm = _get_local_llm()
    try:
        response = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
    except Exception as exc:
        raise LLMUnavailable(f"local LLM call failed: {exc}") from exc

    text = (response["choices"][0]["message"]["content"] or "").strip()
    text = _THINK_RE.sub("", text).strip()  # drop the reasoning block, keep the answer
    if not text:
        raise LLMUnavailable("local LLM returned no text")
    return text
