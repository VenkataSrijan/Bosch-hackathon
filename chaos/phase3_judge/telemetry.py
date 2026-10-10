"""Where the judge's numbers and evidence come from.

LiveSource reads Prometheus, Jaeger and pod logs (kubectl) and removes faults with kubectl.
ReplaySource replays the recordings in phase3_judge/recorded/ on a virtual clock, for --mock.
Both report a metric they could not get as None; nothing here substitutes a default value.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

RECORDED_DIR = Path(__file__).resolve().parent / "recorded"
LABEL_KEY = "chaos.ai/experiment-id"

SUCCESS = "checkout_success_rate_percent"
P99 = "p99_latency_ms"
ERROR = "error_rate_percent"
METRICS = (SUCCESS, P99, ERROR)

# Frontend error rate in percent. "or vector(0)" keeps the ratio defined when no 5xx series exists yet
# (the healthy case); with no traffic at all the denominator is empty and the sample is missing.
ERROR_RATE_QUERY = (
    '(sum(rate(istio_requests_total{destination_workload="frontend",response_code=~"5.."}[30s])) or vector(0))'
    ' / sum(rate(istio_requests_total{destination_workload="frontend"}[30s])) * 100'
)
P99_QUERY = (
    'histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket'
    '{destination_workload="frontend",reporter="destination"}[30s])))'
)

# Mock pacing, in real seconds slept per virtual second. The canary is replayed slowly enough that Phase 2's
# mock canary watch (0.25 s) is over and it is waiting when the verdict lands; the rest runs fast enough that a
# breach shortly after GO lands inside Phase 2's mock watch after re-applying the fault (also 0.25 s).
MOCK_CANARY_SECONDS_PER_TICK = 0.03
MOCK_SECONDS_PER_TICK = 0.005


class EvidenceError(RuntimeError):
    """Traces or logs could not be read."""


@dataclass
class Reading:
    t: datetime
    values: dict[str, Optional[float]]
    problems: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        return [m for m in METRICS if self.values.get(m) is None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- Prometheus

class PrometheusClient:
    def __init__(self, url: str, timeout: float = 5.0, fetch: Callable[[str, float], Any] = _http_get_json):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.fetch = fetch

    def scalar(self, query: str) -> tuple[Optional[float], Optional[str]]:
        """One instant value, or (None, why) when the query fails or returns no usable number."""
        try:
            body = self.fetch(f"{self.url}/api/v1/query?{urllib.parse.urlencode({'query': query})}", self.timeout)
        except Exception as exc:
            return None, f"query failed: {exc}"
        if not isinstance(body, dict) or body.get("status") != "success":
            return None, f"query error: {body.get('error') if isinstance(body, dict) else body}"
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


def read_edge_health(prom: PrometheusClient, t: datetime) -> Reading:
    error, why_error = prom.scalar(ERROR_RATE_QUERY)
    p99, why_p99 = prom.scalar(P99_QUERY)
    problems = [f"error rate: {why_error}"] if why_error else []
    if why_p99:
        problems.append(f"p99: {why_p99}")
    values = {
        SUCCESS: None if error is None else 100.0 - error,
        P99: p99,
        ERROR: error,
    }
    return Reading(t=t, values=values, problems=problems)


# ---------------------------------------------------------------- kubectl

def _kubectl(args: list[str], timeout: float = 30.0) -> tuple[int, str]:
    exe = shutil.which("kubectl")
    if exe is None:
        return 127, "kubectl not found on PATH"
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"kubectl {' '.join(args)} timed out after {timeout:.0f}s"
    return proc.returncode, (proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)).strip()


def delete_fault_args(namespace: str, experiment_id: str) -> list[str]:
    return ["delete", "networkchaos", "-n", namespace, "-l", f"{LABEL_KEY}={experiment_id}", "--now"]


_TS_PREFIX = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d))\s(.*)$")


def parse_log_line(line: str) -> tuple[Optional[datetime], str]:
    """Split a `kubectl logs --timestamps` line into (time, text)."""
    m = _TS_PREFIX.match(line)
    if not m:
        return None, line
    stamp = m.group(1).replace("Z", "+00:00")
    stamp = re.sub(r"\.(\d{6})\d+", r".\1", stamp)  # nanoseconds -> microseconds
    try:
        return datetime.fromisoformat(stamp), m.group(2)
    except ValueError:
        return None, line


# ---------------------------------------------------------------- sources

class LiveSource:
    """Prometheus for health, Jaeger for traces, kubectl for logs and fault removal. Real time."""

    def __init__(self, prometheus_url: str, jaeger_url: str, namespace: str,
                 kubectl: Callable[[list[str]], tuple[int, str]] = _kubectl):
        self.prom = PrometheusClient(prometheus_url)
        self.jaeger_url = jaeger_url.rstrip("/")
        self.namespace = namespace
        self.kubectl = kubectl

    def begin(self, initiated_at: datetime) -> None:
        pass

    def now(self) -> datetime:
        return _utcnow()

    def sleep(self, seconds: float) -> None:
        time.sleep(min(max(seconds, 0.0), 1.0))

    def read(self) -> Reading:
        return read_edge_health(self.prom, self.now())

    def remove_fault(self, experiment_id: str) -> tuple[bool, str]:
        args = delete_fault_args(self.namespace, experiment_id)
        code, out = self.kubectl(args)
        return code == 0, f"kubectl {' '.join(args)}: {out or 'ok'}"

    def traces(self, start: datetime, end: datetime) -> list[dict]:
        try:
            services = _http_get_json(f"{self.jaeger_url}/api/services", 10).get("data") or []
        except Exception as exc:
            raise EvidenceError(f"Jaeger {self.jaeger_url} unreachable: {exc}") from exc
        params = {"start": int(start.timestamp() * 1e6), "end": int(end.timestamp() * 1e6), "limit": 1500}
        traces: dict[str, dict] = {}
        for service in services:
            if service.startswith("jaeger"):
                continue
            query = urllib.parse.urlencode({**params, "service": service})
            try:
                for trace in _http_get_json(f"{self.jaeger_url}/api/traces?{query}", 15).get("data") or []:
                    traces[trace.get("traceID", id(trace))] = trace
            except Exception as exc:
                raise EvidenceError(f"Jaeger traces for {service}: {exc}") from exc
        return list(traces.values())

    def logs(self, service: str, start: datetime, end: datetime) -> list[tuple[str, str]]:
        code, out = self.kubectl(["-n", self.namespace, "get", "pods", "-l", f"app={service}", "-o", "name"])
        if code != 0:
            raise EvidenceError(f"listing {service} pods: {out}")
        lines: list[tuple[str, str]] = []
        for pod in out.split():
            pod = pod.removeprefix("pod/")
            code, text = self.kubectl(["-n", self.namespace, "logs", pod, "--timestamps",
                                       f"--since-time={start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}"])
            if code != 0:
                raise EvidenceError(f"logs of {pod}: {text}")
            for raw in text.splitlines():
                ts, msg = parse_log_line(raw)
                if ts is None or start <= ts <= end:
                    lines.append((pod, msg))
        return lines


class ReplaySource:
    """Replays recorded health, traces and logs for --mock. No cluster, no network.

    The clock is virtual: sleep(1) advances it by one second but really sleeps a few milliseconds,
    so the canary, the breach and the recovery tail keep their recorded spacing in the artifacts.
    """

    def __init__(self, scenario: str, canary_seconds: int, namespace: str = "default",
                 recorded_dir: Path = RECORDED_DIR):
        self.dir = recorded_dir
        self.namespace = namespace
        self.canary_seconds = canary_seconds
        self.series = json.loads((recorded_dir / f"health_{scenario}.json").read_text(encoding="utf-8"))["samples"]
        self.start: Optional[datetime] = None
        self.clock: Optional[datetime] = None

    def begin(self, initiated_at: datetime) -> None:
        self.start = self.clock = initiated_at

    def now(self) -> datetime:
        return self.clock or _utcnow()

    def sleep(self, seconds: float) -> None:
        in_canary = self.start is not None and self.now() < self.start + timedelta(seconds=self.canary_seconds)
        self.clock = self.now() + timedelta(seconds=seconds)
        time.sleep(MOCK_CANARY_SECONDS_PER_TICK if in_canary else MOCK_SECONDS_PER_TICK)

    def read(self) -> Reading:
        offset = (self.now() - (self.start or self.now())).total_seconds()
        entry = self.series[0]
        for candidate in self.series:
            if candidate["offset_s"] <= offset:
                entry = candidate
        values = {m: entry.get(m) for m in (P99, ERROR)}
        values[SUCCESS] = None if values[ERROR] is None else round(100.0 - values[ERROR], 4)
        problems = [f"{m}: no data in recording" for m in (ERROR, P99) if values[m] is None]
        return Reading(t=self.now(), values=values, problems=problems)

    def remove_fault(self, experiment_id: str) -> tuple[bool, str]:
        args = delete_fault_args(self.namespace, experiment_id)
        return True, f"mock: no cluster, skipped kubectl {' '.join(args)}"

    def _shift(self, recorded: dict) -> timedelta:
        then = datetime.fromisoformat(recorded["recorded_initiated_at"].replace("Z", "+00:00"))
        return (self.start or _utcnow()) - then

    def traces(self, start: datetime, end: datetime) -> list[dict]:
        recorded = json.loads((self.dir / "jaeger_traces.json").read_text(encoding="utf-8"))
        shift_us = int(self._shift(recorded).total_seconds() * 1e6)
        lo, hi = start.timestamp() * 1e6, end.timestamp() * 1e6
        out = []
        for trace in recorded["data"]:
            trace = json.loads(json.dumps(trace))
            for span in trace["spans"]:
                span["startTime"] += shift_us
            if any(lo <= s["startTime"] <= hi for s in trace["spans"]):
                out.append(trace)
        return out

    def logs(self, service: str, start: datetime, end: datetime) -> list[tuple[str, str]]:
        recorded = json.loads((self.dir / "pod_logs.json").read_text(encoding="utf-8"))
        shift = self._shift(recorded)
        lines = []
        for pod, raw_lines in (recorded["pods"].get(service) or {}).items():
            for raw in raw_lines:
                ts, msg = parse_log_line(raw)
                if ts is not None and start <= ts + shift <= end:
                    lines.append((pod, msg))
        return lines
