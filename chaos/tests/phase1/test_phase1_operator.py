"""Phase 1: live mode measures or fails (no canned values), and an operator can choose the fault."""
from __future__ import annotations

from pathlib import Path

import pytest

from common.contracts import ExperimentPlan
from common.io import EXPERIMENT_PLAN, read_model
from phase1_observation import profiler
from phase1_observation.__main__ import main, run_observation
from phase1_observation.profiler import ProfilingError, profile_steady_state
from phase1_observation.target_selector import fault_parameters


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def prom(value):
    return {"status": "success", "data": {"result": [{"metric": {}, "value": [0, value]}]}}


def test_live_baseline_is_measured_at_the_frontend(monkeypatch):
    seen = []

    def get(url, params, timeout):
        q = params["query"]
        seen.append(q)
        if 'destination_workload="frontend"' in q and "histogram_quantile" in q:
            return FakeResponse(prom("82.4"))
        if 'destination_workload="frontend"' in q:
            return FakeResponse(prom("0.3"))
        return FakeResponse({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(profiler.requests, "get", get)
    result = profile_steady_state(prometheus_url="http://prom", mock=False)
    base = result.steady_state_baseline
    assert (base.p99_latency_ms, base.global_error_rate_percent, base.checkout_success_rate_percent) == (82.4, 0.3, 99.7)
    # Per-service values Prometheus did not return stay empty instead of taking defaults.
    pay = result.service_profiles["paymentservice"]
    assert pay.p50_latency_ms is None and pay.cpu_saturation_percent is None
    assert any('reporter="destination"' in q for q in seen)


@pytest.mark.parametrize("answer", [{"status": "success", "data": {"result": []}}, prom("NaN")])
def test_live_baseline_without_data_fails_instead_of_using_recorded_numbers(monkeypatch, answer):
    monkeypatch.setattr(profiler.requests, "get", lambda url, params, timeout: FakeResponse(answer))
    with pytest.raises(ProfilingError, match="could not be measured"):
        profile_steady_state(prometheus_url="http://prom", mock=False)


def test_operator_chooses_loss_fault(tmp_path: Path):
    exp = run_observation(tmp_path, mock=True, fault="loss", fault_params={"loss_percent": 40}, duration_seconds=45)
    plan = read_model(exp / EXPERIMENT_PLAN, ExperimentPlan)
    assert (plan.fault_profile.category, plan.fault_profile.action) == ("network", "loss")
    assert plan.fault_profile.parameters == {"loss_percent": 40, "target_pods": 1}
    assert plan.fault_profile.max_duration_seconds == 45


def test_operator_target_needs_two_replicas(tmp_path: Path):
    with pytest.raises(ValueError, match="at least 2 are needed"):
        run_observation(tmp_path, mock=True, target="checkoutservice")


def test_operator_p99_limit_must_exceed_the_injected_delay(tmp_path: Path, capsys):
    assert main(["--out", str(tmp_path), "--mock", "--delay-ms", "300", "--max-p99-ms", "200"]) == 1
    assert "must be above the injected 320 ms" in capsys.readouterr().err
    assert main(["--out", str(tmp_path), "--mock", "--delay-ms", "300", "--max-p99-ms", "400"]) == 0
    plan = read_model(next(tmp_path.glob("exp-*")) / EXPERIMENT_PLAN, ExperimentPlan)
    assert plan.abort_thresholds.max_p99_latency_ms == 400.0


@pytest.mark.parametrize("action,params,error", [
    ("latency", {"delay_ms": 0}, "1 <= delay_ms"),
    ("loss", {"loss_percent": 150}, "1 <= loss_percent"),
    ("pod-kill", {"delay_ms": 10}, "takes no parameter"),
    ("cpu", {}, "unsupported fault"),
])
def test_fault_parameters_are_validated(action, params, error):
    with pytest.raises(ValueError, match=error):
        fault_parameters(action, params)
