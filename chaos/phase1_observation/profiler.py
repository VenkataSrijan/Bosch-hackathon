"""Steady-state profiling over a rolling window (default 15m, configurable down to 2m)."""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from typing import Any, Optional

import requests

from common.contracts import SteadyStateBaseline

from .recorded_data import RECORDED_PROFILING

logger = logging.getLogger(__name__)


@dataclass
class ServiceProfile:
    service_name: str
    p50_latency_ms: Optional[float]
    p90_latency_ms: Optional[float]
    p99_latency_ms: Optional[float]
    traffic_rps: Optional[float]
    error_ratio_percent: Optional[float]
    cpu_saturation_percent: Optional[float]
    memory_saturation_percent: Optional[float]


class ProfilingError(RuntimeError):
    """The live steady-state baseline could not be measured."""


@dataclass
class ProfilingResult:
    steady_state_baseline: SteadyStateBaseline
    service_profiles: dict[str, ServiceProfile]
    window_minutes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "steady_state_baseline": self.steady_state_baseline.model_dump(),
            "service_profiles": {k: asdict(v) for k, v in self.service_profiles.items()},
            "window_minutes": self.window_minutes,
        }


def profile_steady_state(
    prometheus_url: str = "http://localhost:9090",
    window_minutes: int = 15,
    kube_namespace: str = "default",
    mock: bool = False,
) -> ProfilingResult:
    """Collects baseline SLOs and per-service saturation metrics over rolling window."""
    if mock:
        return _profile_mock(window_minutes)

    # Live mode reports only what was measured: no recorded or default values on failure.
    return _profile_live(prometheus_url, window_minutes, kube_namespace)


def _profile_mock(window_minutes: int) -> ProfilingResult:
    base = RECORDED_PROFILING["steady_state_baseline"]
    steady = SteadyStateBaseline(
        p99_latency_ms=float(base["p99_latency_ms"]),
        global_error_rate_percent=float(base["global_error_rate_percent"]),
        checkout_success_rate_percent=float(base["checkout_success_rate_percent"]),
    )
    svc_map = {}
    for svc, metrics in RECORDED_PROFILING.get("services", {}).items():
        svc_map[svc] = ServiceProfile(
            service_name=svc,
            p50_latency_ms=metrics["p50_latency_ms"],
            p90_latency_ms=metrics["p90_latency_ms"],
            p99_latency_ms=metrics["p99_latency_ms"],
            traffic_rps=metrics["traffic_rps"],
            error_ratio_percent=metrics["error_ratio_percent"],
            cpu_saturation_percent=metrics["cpu_saturation_percent"],
            memory_saturation_percent=metrics["memory_saturation_percent"],
        )
    return ProfilingResult(
        steady_state_baseline=steady,
        service_profiles=svc_map,
        window_minutes=window_minutes,
    )


def _prom_scalar(prometheus_url: str, promql: str) -> tuple[float | None, str | None]:
    """One instant value, or (None, why) when the query fails or has no usable number. Never a default."""
    try:
        r = requests.get(f"{prometheus_url}/api/v1/query", params={"query": promql}, timeout=5.0)
        r.raise_for_status()
        body = r.json()
    except (requests.ConnectionError, requests.Timeout) as exc:
        return None, f"cannot reach Prometheus ({type(exc).__name__})"
    except Exception as exc:
        return None, f"query failed: {exc}"
    if body.get("status") != "success":
        return None, f"query error: {body.get('error')}"
    result = (body.get("data") or {}).get("result") or []
    if not result:
        return None, "no data"
    try:
        value = float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return None, f"unreadable value: {exc}"
    if math.isnan(value) or math.isinf(value):
        return None, f"value is {value}"
    return value, None


def edge_queries(window: str) -> dict[str, str]:
    """The frontend edge signals, defined exactly as the Phase 3 judge measures them (over `window`)."""
    return {
        "p99": ("histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket"
                f'{{destination_workload="frontend",reporter="destination"}}[{window}])))'),
        "error": (f'(sum(rate(istio_requests_total{{destination_workload="frontend",response_code=~"5.."}}[{window}]))'
                  f' or vector(0)) / sum(rate(istio_requests_total{{destination_workload="frontend"}}[{window}])) * 100'),
    }


def _profile_live(
    prometheus_url: str,
    window_minutes: int,
    kube_namespace: str,
) -> ProfilingResult:
    """Query live Prometheus for the steady-state baseline. Raises ProfilingError if it cannot be measured."""
    window = f"{max(2, window_minutes)}m"

    # Edge signals: the same frontend p99 and error rate the Phase 3 judge checks against the abort limits.
    q = edge_queries(window)
    p99, why_p99 = _prom_scalar(prometheus_url, q["p99"])
    err, why_err = _prom_scalar(prometheus_url, q["error"])
    problems = [f"frontend p99: {why_p99}"] if why_p99 else []
    if why_err:
        problems.append(f"frontend error rate: {why_err}")
    if problems:
        raise ProfilingError(
            f"live steady-state baseline could not be measured from {prometheus_url} ({'; '.join(problems)}). "
            "Check PROMETHEUS_URL (port-forward svc/prometheus) and that the mesh has served traffic for a few minutes."
        )

    steady = SteadyStateBaseline(
        p99_latency_ms=round(p99, 2),
        global_error_rate_percent=round(err, 4),
        checkout_success_rate_percent=round(100.0 - err, 4),
    )

    # Per-service profiles for the audit file. A value Prometheus did not return stays None.
    services = ["paymentservice", "checkoutservice", "currencyservice", "cartservice"]
    svc_map: dict[str, ServiceProfile] = {}
    for s in services:
        def pq(promql: str) -> float | None:
            value, _ = _prom_scalar(prometheus_url, promql)
            return None if value is None else round(value, 2)

        bucket = f'istio_request_duration_milliseconds_bucket{{destination_workload="{s}"}}[{window}]'
        total = f'istio_requests_total{{destination_workload="{s}"}}[{window}]'
        svc_map[s] = ServiceProfile(
            service_name=s,
            p50_latency_ms=pq(f"histogram_quantile(0.50, sum(rate({bucket})) by (le))"),
            p90_latency_ms=pq(f"histogram_quantile(0.90, sum(rate({bucket})) by (le))"),
            p99_latency_ms=pq(f"histogram_quantile(0.99, sum(rate({bucket})) by (le))"),
            traffic_rps=pq(f"sum(rate({total}))"),
            error_ratio_percent=pq(
                f'(sum(rate(istio_requests_total{{destination_workload="{s}",response_code=~"5.."}}[{window}])) '
                f"or vector(0)) / sum(rate({total})) * 100"),
            cpu_saturation_percent=None,  # not measured
            memory_saturation_percent=None,  # not measured
        )

    return ProfilingResult(
        steady_state_baseline=steady,
        service_profiles=svc_map,
        window_minutes=window_minutes,
    )
