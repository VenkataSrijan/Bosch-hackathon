"""Phase 3 watcher: live PromQL, missing data, the plan's three limits, abort order, canary verdict."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from common.config import FIXTURES_DIR, get_settings
from common.contracts import ExperimentPlan, InjectionReceipt, InjectionStatus, Quarantine
from common.io import (
    ABORT_SIGNAL, HEALTH_SERIES, quarantine_path, read_artifact, read_json, read_model, write_artifact,
)
from phase3_judge.judge import NO_DATA, Judge, check_thresholds
from phase3_judge.telemetry import (
    ERROR, ERROR_RATE_QUERY, P99, SUCCESS, LiveSource, PrometheusClient, Reading, parse_log_line, read_edge_health,
)

T0 = datetime(2026, 10, 11, 0, 50, 15, tzinfo=timezone.utc)


def plan() -> ExperimentPlan:
    return ExperimentPlan.model_validate(read_json(FIXTURES_DIR / "ExperimentPlan.json"))


def prom_body(value):
    return {"status": "success", "data": {"resultType": "vector", "result": [{"metric": {}, "value": [0, value]}]}}


# ---------------------------------------------------------------- PromQL


def test_error_rate_query_is_frontend_5xx_over_all_requests():
    assert 'sum(rate(istio_requests_total{destination_workload="frontend",response_code=~"5.."}[30s]))' \
        in ERROR_RATE_QUERY
    assert ' / sum(rate(istio_requests_total{destination_workload="frontend"}[30s])) * 100' in ERROR_RATE_QUERY


@pytest.mark.parametrize("body,expected,why", [
    (prom_body("0.25"), 0.25, None),
    ({"status": "success", "data": {"result": []}}, None, "no data"),
    (prom_body("NaN"), None, "value is nan"),
    ({"status": "error", "error": "parse error"}, None, "query error: parse error"),
])
def test_prometheus_scalar(body, expected, why):
    value, reason = PrometheusClient("http://prom", fetch=lambda url, timeout: body).scalar("q")
    assert value == expected and reason == why


def test_prometheus_unreachable_is_missing_not_defaulted():
    def boom(url, timeout):
        raise OSError("connection refused")
    reading = read_edge_health(PrometheusClient("http://prom", fetch=boom), T0)
    assert reading.values == {SUCCESS: None, P99: None, ERROR: None}
    assert set(reading.missing) == {SUCCESS, P99, ERROR}
    assert all("connection refused" in p for p in reading.problems)


def test_success_rate_is_100_minus_error_rate():
    answers = {"istio_requests_total": prom_body("0.8"), "histogram_quantile": prom_body("212.5")}
    fetch = lambda url, timeout: next(v for k, v in answers.items() if k in url)  # noqa: E731
    reading = read_edge_health(PrometheusClient("http://prom", fetch=fetch), T0)
    assert reading.values == {SUCCESS: pytest.approx(99.2), P99: 212.5, ERROR: 0.8}


# ---------------------------------------------------------------- thresholds


def test_all_three_plan_limits_are_checked_not_the_global_slo(monkeypatch):
    monkeypatch.setenv("SLO_CHECKOUT_SUCCESS_PERCENT", "99.99")
    th = plan().abort_thresholds  # 500 ms, 0.5 %, 99.5 %
    ok = {SUCCESS: 99.7, ERROR: 0.3, P99: 420.0}
    assert check_thresholds(ok, th) is None  # below the global SLO, inside the plan's limits
    assert check_thresholds({**ok, SUCCESS: 99.4}, th).metric == "min_checkout_success_rate_percent"
    assert check_thresholds({**ok, ERROR: 0.6}, th).metric == "max_error_rate_percent"
    breach = check_thresholds({**ok, P99: 640.0}, th)
    assert (breach.metric, breach.observed, breach.threshold) == ("max_p99_latency_ms", 640.0, 500.0)


# ---------------------------------------------------------------- the watch loop


class ScriptedSource:
    """Plays a list of readings on a virtual clock and acts as Phase 2: the receipt goes final after
    `finish_after` reads, or as soon as the fault is removed."""

    def __init__(self, exp_dir, readings, finish_after=None, removal_ok=True):
        self.exp_dir = exp_dir
        self.readings = list(readings)
        self.finish_after = finish_after
        self.removal_ok = removal_ok
        self.reads = 0
        self.removals = []
        self.clock = T0

    def begin(self, initiated_at):
        self.clock = initiated_at

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def _finish(self, status):
        receipt = read_model(self.exp_dir / "InjectionReceipt.json", InjectionReceipt)
        if not receipt.is_final:
            receipt.status = status
            receipt.timestamps.ended_at = self.clock
            write_artifact(self.exp_dir, receipt)

    def read(self):
        values = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        if self.finish_after is not None and self.reads >= self.finish_after:
            self._finish(InjectionStatus.COMPLETED)
        missing = [m for m in (P99, ERROR) if values.get(m) is None]
        full = dict(values)
        full[SUCCESS] = None if values.get(ERROR) is None else 100 - values[ERROR]
        return Reading(t=self.clock, values=full, problems=[f"{m}: no data" for m in missing])

    def remove_fault(self, experiment_id):
        # The judge must remove the fault before it announces the abort.
        self.removals.append({"abort_signal_existed": (self.exp_dir / ABORT_SIGNAL).exists(),
                              "quarantine_existed": quarantine_path(self.exp_dir).exists()})
        self._finish(InjectionStatus.ABORTED)
        return self.removal_ok, "fake kubectl"

    def traces(self, start, end):
        return []

    def logs(self, service, start, end):
        return []


def run_judge(tmp_path, readings, **kw):
    exp = tmp_path / "artifacts" / "exp-boutique-pay-089"
    p = plan()
    write_artifact(exp, p)
    receipt = InjectionReceipt.model_validate(copy.deepcopy(read_json(FIXTURES_DIR / "InjectionReceipt.json")))
    receipt.timestamps.initiated_at = T0
    receipt.timestamps.hard_timeout_at = T0 + timedelta(seconds=60)
    write_artifact(exp, receipt)
    source = ScriptedSource(exp, readings, **kw)
    logs = []
    code = Judge(exp, p, source, get_settings(), mock=True, log=logs.append).run()
    return exp, source, code, logs


HEALTHY = {P99: 20.0, ERROR: 0.02}
MISSING = {P99: None, ERROR: None}


def test_healthy_run_is_steady_state_with_go_canary(tmp_path):
    exp, source, code, _ = run_judge(tmp_path, [HEALTHY], finish_after=40)
    assert code == 0
    assert read_artifact(exp, "CanaryVerdict.json").decision == "GO"
    assert not (exp / ABORT_SIGNAL).exists() and not quarantine_path(exp).exists()
    rca = read_artifact(exp, "RootCauseAnalysis.json")
    assert rca.verdict == "STEADY_STATE_MAINTAINED" and rca.cascade_path == [] and rca.root_cause_isolation is None
    assert rca.impact_summary.peak_p99_latency_ms == 20.0
    assert source.removals == []


def test_breach_after_go_removes_fault_then_signals(tmp_path):
    readings = [HEALTHY] * 13 + [{P99: 650.0, ERROR: 0.05}]
    exp, source, _, _ = run_judge(tmp_path, readings)
    verdict = read_artifact(exp, "CanaryVerdict.json")
    signal = read_artifact(exp, "AbortSignal.json")
    assert verdict.decision == "GO" and signal.aborted_at > verdict.checked_at
    assert (signal.breached_metric, signal.observed_value, signal.threshold) == ("max_p99_latency_ms", 650.0, 500.0)
    assert source.removals == [{"abort_signal_existed": False, "quarantine_existed": False}]
    assert "fake kubectl" in read_model(quarantine_path(exp), Quarantine).reason
    assert read_artifact(exp, "RootCauseAnalysis.json").verdict == "ABORT_TRIGGERED"


def test_breach_during_canary_is_no_go_and_abort(tmp_path):
    exp, source, _, _ = run_judge(tmp_path, [HEALTHY, HEALTHY, {P99: 30.0, ERROR: 0.9}])
    assert read_artifact(exp, "CanaryVerdict.json").decision == "NO_GO"
    assert read_artifact(exp, "AbortSignal.json").breached_metric == "min_checkout_success_rate_percent"
    assert len(source.removals) == 1


def test_two_missing_samples_are_tolerated(tmp_path):
    exp, source, _, _ = run_judge(tmp_path, [HEALTHY, MISSING, MISSING, HEALTHY], finish_after=30)
    assert not (exp / ABORT_SIGNAL).exists() and source.removals == []
    record = read_json(exp / "JudgeLog.json")
    assert len(record["missing_samples"]) == 2


def test_three_missing_samples_abort_fail_safe(tmp_path):
    exp, source, _, _ = run_judge(tmp_path, [HEALTHY, MISSING, MISSING, MISSING])
    signal = read_artifact(exp, "AbortSignal.json")
    assert signal.observed_value == NO_DATA
    assert source.removals == [{"abort_signal_existed": False, "quarantine_existed": False}]
    # Missing samples never enter HealthSeries.json with made-up values.
    series = read_artifact(exp, HEALTH_SERIES).root
    assert all(s.p99_latency_ms == 20.0 for s in series)
    assert "no data for 3 consecutive samples" in read_json(quarantine_path(exp))["reason"]


def test_failed_fault_removal_is_reported(tmp_path):
    exp, _, _, logs = run_judge(tmp_path, [{P99: 900.0, ERROR: 0.02}], removal_ok=False)
    assert "fault removal FAILED" in read_json(quarantine_path(exp))["reason"]
    assert (exp / ABORT_SIGNAL).exists()  # Phase 2 still gets told to stop


# ---------------------------------------------------------------- kubectl


def test_live_abort_runs_the_label_delete():
    calls = []
    source = LiveSource("http://prom", "http://jaeger", "default",
                        kubectl=lambda args: calls.append(args) or (0, 'networkchaos "x" deleted'))
    ok, how = source.remove_fault("exp-boutique-pay-089")
    assert ok and calls == [["delete", "networkchaos", "-n", "default", "-l",
                             "chaos.ai/experiment-id=exp-boutique-pay-089", "--now"]]


def test_log_timestamp_parsing():
    ts, text = parse_log_line("2026-10-11T00:50:20.123456789Z rpc error: code = DeadlineExceeded")
    assert ts == datetime(2026, 10, 11, 0, 50, 20, 123456, tzinfo=timezone.utc)
    assert text == "rpc error: code = DeadlineExceeded"
    assert parse_log_line("no stamp") == (None, "no stamp")
