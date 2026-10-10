"""Phase 3 contract and integration tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from common.config import CHAOS_ROOT, FIXTURES_DIR
from common.contracts import (
    AbortSignal,
    ARTIFACT_MODELS, CanaryVerdict, ExperimentPlan, HealthSample, HealthSeries,
    InjectionReceipt, RootCauseAnalysis, RootCauseIsolation,
)
from common.io import read_artifact, read_model, write_artifact


def fixture(name: str) -> dict:
    import copy
    return copy.deepcopy(json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", sorted(ARTIFACT_MODELS))
def test_fixture_validates_and_round_trips(name):
    model = ARTIFACT_MODELS[name].model_validate(fixture(name))
    again = ARTIFACT_MODELS[name].model_validate(model.model_dump())
    assert model.model_dump() == again.model_dump()


def test_every_artifact_has_a_fixture():
    assert {p.name for p in FIXTURES_DIR.glob("*.json")} == set(ARTIFACT_MODELS)


def test_canary_verdict_model():
    data = fixture("CanaryVerdict.json")
    data["experiment_id"] = "exp-test-001"
    verdict = CanaryVerdict.model_validate(data)
    assert verdict.experiment_id == "exp-test-001"
    assert verdict.decision in ("GO", "NO_GO")


def test_abort_signal_model():
    data = fixture("AbortSignal.json")
    data["experiment_id"] = "exp-test-001"
    signal = AbortSignal.model_validate(data)
    assert signal.experiment_id == "exp-test-001"


def test_health_sample_model():
    sample = HealthSample(
        t="2026-10-11T00:50:15Z",
        checkout_success_rate_percent=99.98,
        p99_latency_ms=18.5,
        error_rate_percent=0.02,
    )
    d = sample.model_dump()
    # Verify model dump works and core fields are present
    assert "checkout_success_rate_percent" in d
    assert "p99_latency_ms" in d
    assert "error_rate_percent" in d
    # t should be a datetime-like value
    assert d["t"] is not None


def test_health_series_model():
    samples = [HealthSample(
        t="2026-10-11T00:50:15Z",
        checkout_success_rate_percent=99.98,
        p99_latency_ms=18.5,
        error_rate_percent=0.02,
    )]
    series = HealthSeries(root=samples)
    d = series.model_dump()
    # HealthSeries is a RootModel, so model_dump() returns the inner list directly
    assert len(d) == 1


def test_root_cause_analysis_steady_state():
    """STEADY_STATE_MAINTAINED with empty cascade_path and no root_cause_isolation."""
    data = fixture("RootCauseAnalysis.json")
    data["verdict"] = "STEADY_STATE_MAINTAINED"
    data["impact_summary"]["steady_state_maintained"] = True
    data["cascade_path"] = []
    data["root_cause_isolation"] = None
    # Re-add experiment_id
    data["experiment_id"] = "exp-test-001"
    rca = RootCauseAnalysis.model_validate(data)
    assert rca.verdict == "STEADY_STATE_MAINTAINED"
    assert rca.cascade_path == []
    assert rca.root_cause_isolation is None


def test_root_cause_analysis_degraded_no_abort():
    """DEGRADED_NO_ABORT when no abort but metrics were affected."""
    data = fixture("RootCauseAnalysis.json")
    data["verdict"] = "DEGRADED_NO_ABORT"
    data["impact_summary"]["steady_state_maintained"] = False
    data["cascade_path"] = []
    data["root_cause_isolation"] = None
    data["experiment_id"] = "exp-test-001"
    rca = RootCauseAnalysis.model_validate(data)
    assert rca.verdict == "DEGRADED_NO_ABORT"


def test_root_cause_analysis_abort_triggered():
    """ABORT_TRIGGERED with cascade and root cause isolation."""
    data = fixture("RootCauseAnalysis.json")
    data["verdict"] = "ABORT_TRIGGERED"
    data["impact_summary"]["steady_state_maintained"] = False
    data["experiment_id"] = "exp-test-001"
    rca = RootCauseAnalysis.model_validate(data)
    assert rca.verdict == "ABORT_TRIGGERED"


def test_rca_with_cascade_path():
    """RCA with a multi-hop cascade path."""
    from common.contracts import CascadeHop
    data = fixture("RootCauseAnalysis.json")
    data["verdict"] = "ABORT_TRIGGERED"
    data["cascade_path"] = [
        CascadeHop(hop=1, service="paymentservice", manifestation="latency injected"),
        CascadeHop(hop=2, service="checkoutservice", manifestation="RPC blocked"),
        CascadeHop(hop=3, service="frontend", manifestation="504 timeout"),
    ]
    data["root_cause_isolation"] = {
        "faulty_service": "checkoutservice",
        "mechanism": "no deadline",
        "stack_trace_snippet": "rpc error: code = DeadlineExceeded",
    }
    data["experiment_id"] = "exp-test-001"
    rca = RootCauseAnalysis.model_validate(data)
    assert len(rca.cascade_path) == 3
    assert rca.root_cause_isolation.faulty_service == "checkoutservice"


def test_mock_pipeline_end_to_end(tmp_path):  # type: ignore
    """Run the full pipeline with --mock and verify Phase 3 produces all artifacts."""
    result = subprocess.run(
        [sys.executable, "run_pipeline.py", "--mock", "--artifacts", str(tmp_path)],
        cwd=CHAOS_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exp_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(exp_dirs) == 1
    exp_dir = exp_dirs[0]

    # Verify all expected artifacts exist
    for name in ("ExperimentPlan.json", "InjectionReceipt.json", "CanaryVerdict.json",
                 "AbortSignal.json", "HealthSeries.json", "RootCauseAnalysis.json"):
        artifact = read_artifact(exp_dir, name)
        assert artifact is not None, f"{name} missing"

    # Verify receipt is final
    receipt = read_artifact(exp_dir, "InjectionReceipt.json")
    assert receipt is not None
    assert receipt.is_final

    # Verify RCA verdict is valid
    rca = read_artifact(exp_dir, "RootCauseAnalysis.json")
    assert rca is not None
    assert rca.verdict in ("ABORT_TRIGGERED", "DEGRADED_NO_ABORT", "STEADY_STATE_MAINTAINED")

    # Verify HealthSeries has samples
    hs = read_artifact(exp_dir, "HealthSeries.json")
    assert hs is not None
    assert len(hs.root) > 0