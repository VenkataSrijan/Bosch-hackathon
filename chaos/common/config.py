"""Runtime configuration, read from environment variables with fixed defaults.

Every phase reads settings through `get_settings()` so that the defaults live in one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CHAOS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = CHAOS_ROOT.parent
FIXTURES_DIR = CHAOS_ROOT / "fixtures"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass(frozen=True)
class Settings:
    kube_namespace: str
    prometheus_url: str
    jaeger_url: str
    artifacts_dir: Path
    slo_checkout_success_percent: float
    safety_alpha: float
    canary_seconds: int
    canary_verdict_timeout_seconds: int
    llm_provider: str
    llm_api_key: str
    llm_model: str


def get_settings() -> Settings:
    artifacts = Path(_env("ARTIFACTS_DIR", "artifacts"))
    if not artifacts.is_absolute():
        artifacts = CHAOS_ROOT / artifacts
    return Settings(
        kube_namespace=_env("KUBE_NAMESPACE", "default"),
        prometheus_url=_env("PROMETHEUS_URL", "http://localhost:9090"),
        jaeger_url=_env("JAEGER_URL", "http://localhost:16686/jaeger"),  # Istio addon base path
        artifacts_dir=artifacts,
        slo_checkout_success_percent=float(_env("SLO_CHECKOUT_SUCCESS_PERCENT", "99.5")),
        safety_alpha=float(_env("SAFETY_ALPHA", "0.10")),
        canary_seconds=int(_env("CANARY_SECONDS", "10")),
        canary_verdict_timeout_seconds=int(_env("CANARY_VERDICT_TIMEOUT_SECONDS", "30")),
        llm_provider=_env("LLM_PROVIDER", "").lower(),
        llm_api_key=_env("LLM_API_KEY", ""),
        llm_model=_env("LLM_MODEL", "claude-opus-5-5"),
    )
