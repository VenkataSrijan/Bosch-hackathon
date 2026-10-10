"""Phase 3 root-cause evidence: cascade and isolation come from traces and logs, never fixed text."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

from common.config import FIXTURES_DIR
from common.contracts import ExperimentPlan, InjectionReceipt
from common.io import read_json
from phase3_judge.evidence import analyze, calls_from_traces
from phase3_judge.telemetry import EvidenceError, ReplaySource

T0 = datetime(2026, 10, 11, 0, 50, 15, tzinfo=timezone.utc)


def plan(target="paymentservice") -> ExperimentPlan:
    data = copy.deepcopy(read_json(FIXTURES_DIR / "ExperimentPlan.json"))
    data["target_node"].update(service_name=target, deployment=target)
    return ExperimentPlan.model_validate(data)


def receipt() -> InjectionReceipt:
    return InjectionReceipt.model_validate(read_json(FIXTURES_DIR / "InjectionReceipt.json"))


def span_pair(caller, callee, offset_s, ms, grpc="0", flags="-", n=[0]):
    n[0] += 1
    start = int((T0 + timedelta(seconds=offset_s)).timestamp() * 1e6)
    tags = [{"key": "grpc.status_code", "value": grpc}, {"key": "response_flags", "value": flags},
            {"key": "http.status_code", "value": "200"}]
    host = f"{callee}.default.svc.cluster.local"
    return {"traceID": f"t{n[0]}", "processes": {"p1": {"serviceName": f"{caller}.default"},
                                                  "p2": {"serviceName": f"{callee}.default"}},
            "spans": [{"spanID": f"c{n[0]}", "startTime": start, "duration": int(ms * 1000), "processID": "p1",
                       "operationName": f"{host}:50051/*", "references": [],
                       "tags": tags + [{"key": "span.kind", "value": "client"},
                                       {"key": "upstream_cluster", "value": f"outbound|50051||{host}"}]},
                      {"spanID": f"s{n[0]}", "startTime": start + 500, "duration": 1000, "processID": "p2",
                       "operationName": f"{host}:50051/*",
                       "references": [{"refType": "CHILD_OF", "spanID": f"c{n[0]}"}],
                       "tags": [{"key": "span.kind", "value": "server"}]}]}


class FakeEvidence:
    def __init__(self, traces=None, logs=None, trace_error=None, log_error=None):
        self._traces, self._logs = traces or [], logs or {}
        self.trace_error, self.log_error = trace_error, log_error

    def traces(self, start, end):
        if self.trace_error:
            raise EvidenceError(self.trace_error)
        return self._traces

    def logs(self, service, start, end):
        if self.log_error:
            raise EvidenceError(self.log_error)
        return self._logs.get(service, [])


def timeout_scenario():
    """cartservice is slow; checkoutservice gives up with DEADLINE_EXCEEDED and frontend gets the error."""
    traces = []
    for i in range(-25, 0, 2):  # baseline
        traces += [span_pair("checkoutservice", "cartservice", i, 4), span_pair("frontend", "checkoutservice", i, 30)]
    for i in range(1, 20, 2):  # fault window
        traces += [span_pair("checkoutservice", "cartservice", i, 200, grpc="4", flags="UT"),
                   span_pair("frontend", "checkoutservice", i, 215, grpc="13")]
    logs = {"checkoutservice": [("checkoutservice-abc", "rpc error: code = DeadlineExceeded desc = context "
                                                         "deadline exceeded"),
                                ("checkoutservice-abc", '{"severity":"info","message":"[PlaceOrder] ok"}')]}
    return traces, logs


def test_recorded_mock_traces_give_cascade_and_isolation():
    source = ReplaySource("abort", 10)
    source.begin(T0)
    found = analyze(source, plan(), receipt(), T0, T0 + timedelta(seconds=17))
    assert [h.service for h in found.cascade_path] == ["paymentservice", "checkoutservice", "frontend"]
    assert "calls from checkoutservice" in found.cascade_path[0].manifestation
    assert found.root_cause.faulty_service == "checkoutservice"
    assert "cut short by a deadline" in found.root_cause.mechanism
    assert found.root_cause.stack_trace_snippet is None  # the recording has no error lines
    assert "main.go" not in found.root_cause.mechanism


def test_timeouts_and_log_snippet_come_from_the_data():
    traces, logs = timeout_scenario()
    found = analyze(FakeEvidence(traces, logs), plan("cartservice"), receipt(), T0, T0 + timedelta(seconds=20))
    assert [h.service for h in found.cascade_path] == ["cartservice", "checkoutservice", "frontend"]
    assert "grpc 4 DEADLINE_EXCEEDED / envoy UT x10" in found.cascade_path[0].manifestation
    assert found.root_cause.faulty_service == "checkoutservice"
    assert "10 timeouts" in found.root_cause.mechanism and "It reached frontend" in found.root_cause.mechanism
    assert found.root_cause.stack_trace_snippet == ("checkoutservice-abc: rpc error: code = DeadlineExceeded "
                                                    "desc = context deadline exceeded")


def test_no_evidence_is_said_in_the_fields():
    source = FakeEvidence(trace_error="Jaeger http://localhost:16686 unreachable", log_error="forbidden")
    found = analyze(source, plan(), receipt(), T0, T0 + timedelta(seconds=20))
    assert len(found.cascade_path) == 1 and found.cascade_path[0].service == "paymentservice"
    assert "no Jaeger traces" in found.cascade_path[0].manifestation
    assert found.root_cause.mechanism.startswith("No evidence of propagation found")
    assert "Root cause not isolated" in found.root_cause.mechanism
    assert found.root_cause.stack_trace_snippet is None
    assert any("forbidden" in n for n in found.notes)


def test_fault_contained_by_the_caller_stops_the_cascade():
    traces = []
    for i in range(-25, 0, 2):
        traces += [span_pair("checkoutservice", "paymentservice", i, 3), span_pair("frontend", "checkoutservice", i, 30)]
    for i in range(1, 20, 2):
        traces += [span_pair("checkoutservice", "paymentservice", i, 260), span_pair("frontend", "checkoutservice", i, 31)]
    found = analyze(FakeEvidence(traces), plan(), receipt(), T0, T0 + timedelta(seconds=20))
    assert [h.service for h in found.cascade_path] == ["paymentservice", "checkoutservice"]
    assert "the fault stopped here" in found.cascade_path[1].manifestation
    assert "Its own callers were not affected" in found.root_cause.mechanism


def test_client_span_names_the_callee():
    calls = calls_from_traces([span_pair("frontend", "checkoutservice", 1, 12, grpc="14")])
    assert [(c.caller, c.callee, c.duration_ms, c.status) for c in calls] == \
        [("frontend", "checkoutservice", 12.0, "grpc 14 UNAVAILABLE")]
