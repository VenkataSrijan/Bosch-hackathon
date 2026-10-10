"""Root-cause evidence from Jaeger traces and pod logs, limited to the experiment window.

Every RPC becomes a Call (caller -> callee, duration, failure status), taken from the caller's client
span (what the caller waited for) or, when there is none, from the callee's server span. Calls are
grouped per edge and split into baseline (before injection) and fault (injection to end). The cascade
walks from the injected service outward along edges whose fault-window calls are slow or failing
compared with their own baseline. Nothing in the output is fixed text about a particular service:
when there is no evidence, the fields say so.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from common.contracts import CascadeHop, ExperimentPlan, InjectionReceipt, RootCauseIsolation

from .telemetry import EvidenceError

TRAFFIC_SOURCES = {"loadgenerator", "(edge)"}  # callers that are clients, never a cascade hop
GRPC_CODES = {1: "CANCELLED", 2: "UNKNOWN", 4: "DEADLINE_EXCEEDED", 8: "RESOURCE_EXHAUSTED",
              13: "INTERNAL", 14: "UNAVAILABLE"}
ERROR_LINE = re.compile(
    r"(?i)\b(rpc error|deadline ?exceeded|context deadline|unavailable|panic|traceback|exception)\b"
    r'|"severity"\s*:\s*"(error|critical)"|\blevel=(error|fatal)\b'
)
SNIPPET_LINES = 3
SNIPPET_LINE_CHARS = 240


@dataclass
class Call:
    caller: str
    callee: str
    start: datetime
    duration_ms: float
    status: str  # "" when the call succeeded

    @property
    def failed(self) -> bool:
        return bool(self.status)


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)] if ordered else 0.0


@dataclass
class Edge:
    caller: str
    callee: str
    base: list[Call] = field(default_factory=list)
    fault: list[Call] = field(default_factory=list)

    def p(self, q: float, which: str = "fault") -> float:
        return _pct([c.duration_ms for c in getattr(self, which)], q)

    @property
    def failures(self) -> Counter:
        return Counter(c.status for c in self.fault if c.failed)

    def slow_limit_ms(self, default_ms: float) -> float:
        if len(self.base) >= 3:
            base_p95 = self.p(0.95, "base")
            return max(2 * base_p95, base_p95 + 25)
        return default_ms

    def is_slow(self, default_ms: float) -> bool:
        return bool(self.fault) and self.p(0.95) > self.slow_limit_ms(default_ms)

    def is_failing(self) -> bool:
        failed = sum(self.failures.values())
        base_rate = sum(c.failed for c in self.base) / len(self.base) if self.base else 0.0
        return failed >= 2 and failed / len(self.fault) > base_rate + 0.01

    def anomalous(self, default_ms: float) -> bool:
        return self.is_slow(default_ms) or self.is_failing()

    def describe(self) -> str:
        base = (f"baseline p95 {self.p(0.95, 'base'):.0f} ms over {len(self.base)} calls" if self.base
                else "no baseline calls")
        text = (f"{len(self.fault)} calls from {self.caller}: p95 {self.p(0.95):.0f} ms ({base}), "
                f"max {self.p(1.0):.0f} ms")
        failed = self.failures
        if failed:
            text += f", {sum(failed.values())} failed (" + ", ".join(f"{s} x{n}" for s, n in failed.most_common()) + ")"
        else:
            text += ", none failed"
        return text


# ---------------------------------------------------------------- spans -> calls

def _tags(span: dict) -> dict[str, Any]:
    return {t.get("key"): t.get("value") for t in span.get("tags") or []}


def _service_from_host(text: Any) -> Optional[str]:
    if not text or not isinstance(text, str):
        return None
    if "|" in text:  # Envoy cluster name: outbound|50051||paymentservice.default.svc.cluster.local
        parts = text.split("|")
        text = parts[3] if len(parts) > 3 else ""
    host = re.split(r"[:/]", text.strip(), maxsplit=1)[0]
    if not host or re.fullmatch(r"[\d.]+", host):
        return None
    return host.split(".")[0] or None


def _status(tags: dict[str, Any]) -> str:
    parts = []
    grpc = tags.get("grpc.status_code")
    if grpc not in (None, "", 0, "0"):
        try:
            code = int(grpc)
            parts.append(f"grpc {code} {GRPC_CODES.get(code, '')}".strip())
        except (TypeError, ValueError):
            parts.append(f"grpc {grpc}")
    http = tags.get("http.status_code")
    try:
        if http is not None and int(http) >= 500:
            parts.append(f"http {int(http)}")
    except (TypeError, ValueError):
        pass
    flags = tags.get("response_flags")
    if flags not in (None, "", "-"):
        parts.append(f"envoy {flags}")
    if not parts and str(tags.get("error", "")).lower() == "true":
        parts.append("error")
    return " / ".join(parts)


def calls_from_traces(traces: list[dict]) -> list[Call]:
    calls: list[Call] = []
    for trace in traces:
        spans = trace.get("spans") or []
        processes = trace.get("processes") or {}
        by_id = {s.get("spanID"): s for s in spans}

        def service_of(span: dict) -> str:
            tags = _tags(span)
            if tags.get("istio.canonical_service"):
                return str(tags["istio.canonical_service"])
            name = (processes.get(span.get("processID")) or {}).get("serviceName") or "unknown"
            return name.split(".")[0]

        def parent_of(span: dict) -> Optional[dict]:
            for ref in span.get("references") or []:
                if ref.get("refType", "CHILD_OF") == "CHILD_OF" and ref.get("spanID") in by_id:
                    return by_id[ref["spanID"]]
            return None

        children: dict[str, list[dict]] = {}
        for span in spans:
            parent = parent_of(span)
            if parent is not None:
                children.setdefault(parent.get("spanID"), []).append(span)

        for span in spans:
            tags = _tags(span)
            kind = tags.get("span.kind")
            me = service_of(span)
            start = datetime.fromtimestamp(span.get("startTime", 0) / 1e6, tz=timezone.utc)
            duration = span.get("duration", 0) / 1000.0
            if kind == "client":
                callee = (_service_from_host(tags.get("upstream_cluster"))
                          or _service_from_host(tags.get("peer.service"))
                          or _service_from_host(tags.get("net.peer.name"))
                          or next((service_of(c) for c in children.get(span.get("spanID"), [])
                                   if _tags(c).get("span.kind") == "server"), None)
                          or _service_from_host(span.get("operationName")))
                if callee and callee != me:
                    calls.append(Call(me, callee, start, duration, _status(tags)))
            elif kind == "server":
                parent = parent_of(span)
                if parent is not None and _tags(parent).get("span.kind") == "client":
                    continue  # already counted from the caller's side
                caller = service_of(parent) if parent is not None else "(edge)"
                if caller != me:
                    calls.append(Call(caller, me, start, duration, _status(tags)))
    return calls


def build_edges(calls: list[Call], injected_at: datetime, ended_at: datetime) -> dict[tuple[str, str], Edge]:
    edges: dict[tuple[str, str], Edge] = {}
    for call in calls:
        edge = edges.setdefault((call.caller, call.callee), Edge(call.caller, call.callee))
        if call.start < injected_at:
            edge.base.append(call)
        elif call.start <= ended_at:
            edge.fault.append(call)
    return edges


# ---------------------------------------------------------------- cascade and root cause

@dataclass
class Findings:
    cascade_path: list[CascadeHop]
    root_cause: RootCauseIsolation
    notes: list[str]
    edges: list[dict]


def _fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%H:%M:%SZ")


def _cascade(edges: dict, target: str, fault_text: str, default_ms: float, span_note: str) -> tuple[list[str], list[str]]:
    """Returns (services in order from the injected one outward, manifestation per service)."""
    path, texts = [target], []
    current = target
    while True:
        inbound = sorted((e for (_, callee), e in edges.items() if callee == current and e.anomalous(default_ms)),
                         key=lambda e: len(e.fault), reverse=True)
        if not inbound:
            if current == target:
                texts.append(f"{fault_text}; no slow or failed calls into {target} found ({span_note})")
            else:
                texts.append(f"slowed by calls to {path[-2]} (above), but no slow or failed calls into "
                             f"{current} from its callers: the fault stopped here")
            break
        described = "; ".join(e.describe() for e in inbound[:2])
        texts.append(f"{fault_text}. Calls into it: {described}" if current == target else described)
        onward = [e for e in inbound if e.caller not in path and e.caller not in TRAFFIC_SOURCES]
        if not onward:
            break
        current = onward[0].caller
        path.append(current)
    return path, texts


def _mechanism(edges: dict, path: list[str], default_ms: float) -> tuple[str, str]:
    target = path[0]
    if len(path) == 1:
        inbound = [e for (_, callee), e in edges.items() if callee == target and e.anomalous(default_ms)]
        if inbound:
            return target, (f"Calls into {target} were slow or failing ({inbound[0].describe()}), and the only "
                            f"callers affected are traffic sources; no service between {target} and the edge.")
        return target, ""
    faulty, edge = path[1], edges[(path[1], target)]
    onward = edges.get((path[2], faulty)) if len(path) > 2 else None
    spread = (f" It reached {path[2]}: its calls to {faulty} had p95 {onward.p(0.95):.0f} ms (baseline p95 "
              f"{onward.p(0.95, 'base'):.0f} ms), {sum(onward.failures.values())} failed." if onward
              else " Its own callers were not affected.")
    failures = edge.failures
    timeouts = sum(n for status, n in failures.items() if "DEADLINE_EXCEEDED" in status or "envoy UT" in status)
    if timeouts:
        return faulty, (f"{faulty} calls to {target} ended in {timeouts} timeouts (p50 {edge.p(0.5):.0f} ms) and the "
                        f"error was returned to its caller instead of being absorbed by a fallback.{spread}")
    if failures:
        statuses = ", ".join(f"{s} x{n}" for s, n in failures.most_common())
        return faulty, (f"{faulty} calls to {target} failed ({statuses}) with no retry or fallback hiding the "
                        f"failure.{spread}")
    return faulty, (f"{faulty} waited on {target} for p95 {edge.p(0.95):.0f} ms (baseline p95 "
                    f"{edge.p(0.95, 'base'):.0f} ms); none of its {len(edge.fault)} calls was cut short by a "
                    f"deadline (no DEADLINE_EXCEEDED or Envoy UT), so the delay passed straight through.{spread}")


def _log_evidence(source: Any, services: list[str], start: datetime, end: datetime) -> tuple[dict, list[str], Optional[str]]:
    """Error lines per service, notes, and a snippet of real lines (None when there are none)."""
    per_service: dict[str, list[tuple[str, str]]] = {}
    notes: list[str] = []
    for service in services:
        try:
            lines = source.logs(service, start, end)
        except EvidenceError as exc:
            notes.append(f"pod logs for {service} unavailable: {exc}")
            continue
        per_service[service] = [(pod, text) for pod, text in lines if ERROR_LINE.search(text)]
    snippet_lines = []
    for service in services:
        for pod, text in per_service.get(service, []):
            if len(snippet_lines) < SNIPPET_LINES:
                snippet_lines.append(f"{pod}: {text.strip()[:SNIPPET_LINE_CHARS]}")
    return per_service, notes, ("\n".join(snippet_lines) or None)


def analyze(source: Any, plan: ExperimentPlan, receipt: InjectionReceipt,
            injected_at: datetime, ended_at: datetime) -> Findings:
    target = plan.target_node.service_name
    window_start, window_end = injected_at - timedelta(seconds=30), ended_at + timedelta(seconds=30)
    window = f"{_fmt(window_start)}-{_fmt(window_end)}"
    fault_text = (f"{receipt.execution_details.fault_applied} on {receipt.execution_details.target_pod} "
                  f"({receipt.execution_details.pod_scope})")
    default_ms = max(50.0, 2 * plan.steady_state_baseline.p99_latency_ms)
    notes: list[str] = []

    try:
        traces = source.traces(window_start, window_end)
        calls = calls_from_traces(traces)
        span_note = f"{len(traces)} Jaeger traces, {len(calls)} calls in {window}"
    except EvidenceError as exc:
        traces, calls = [], []
        span_note = f"no Jaeger traces for {window}: {exc}"
    notes.append(span_note)
    edges = build_edges(calls, injected_at, ended_at)

    path, texts = _cascade(edges, target, fault_text, default_ms, span_note)
    per_service, log_notes, snippet = _log_evidence(source, path, window_start, window_end)
    notes.extend(log_notes)
    hops = []
    for i, (service, text) in enumerate(zip(path, texts), start=1):
        if service in per_service:
            n = len(per_service[service])
            text += f"; pod logs: {n} error line{'s' if n != 1 else ''}" if n else "; pod logs: no error lines"
        hops.append(CascadeHop(hop=i, service=service, manifestation=text))

    faulty, mechanism = _mechanism(edges, path, default_ms)
    if not mechanism:
        found_logs = any(per_service.values())
        mechanism = (f"No evidence of propagation found: {span_note}"
                     + ("; error lines in pod logs, see snippet" if found_logs else "; no error lines in pod logs")
                     + ". Root cause not isolated.")
    return Findings(
        cascade_path=hops,
        root_cause=RootCauseIsolation(faulty_service=faulty, mechanism=mechanism, stack_trace_snippet=snippet),
        notes=notes,
        edges=[{"caller": e.caller, "callee": e.callee, "baseline_calls": len(e.base), "fault_calls": len(e.fault),
                "baseline_p95_ms": round(e.p(0.95, "base"), 1), "fault_p95_ms": round(e.p(0.95), 1),
                "fault_failures": dict(e.failures), "anomalous": e.anomalous(default_ms)}
               for e in edges.values()],
    )
