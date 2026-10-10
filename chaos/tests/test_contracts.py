"""Shared contract tests: fixtures validate, schemas are current, invariants hold."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from common.config import CHAOS_ROOT, FIXTURES_DIR
from common.contracts import (
    ARTIFACT_MODELS, ExperimentPlan, InjectionReceipt, Priority, Quarantine, Recommendation, RootCauseAnalysis,
)
from common.io import dump_model, read_artifact, read_json, write_artifact, write_model
from common.schemas import CONTRACTS_DIR, render_schemas


def fixture(name: str) -> dict:
    return copy.deepcopy(read_json(FIXTURES_DIR / name))


@pytest.mark.parametrize("name", sorted(ARTIFACT_MODELS))
def test_fixture_validates_and_round_trips(name):
    model = ARTIFACT_MODELS[name].model_validate(fixture(name))
    again = ARTIFACT_MODELS[name].model_validate(dump_model(model))
    assert dump_model(again) == dump_model(model)


def test_every_artifact_has_a_fixture():
    assert {p.name for p in FIXTURES_DIR.glob("*.json")} == set(ARTIFACT_MODELS)


def test_generated_schemas_are_current():
    for filename, text in render_schemas().items():
        path = CONTRACTS_DIR / filename
        assert path.exists(), f"missing {filename}: run python -m common.schemas"
        assert path.read_text(encoding="utf-8") == text, f"stale {filename}: run python -m common.schemas"


def test_quarantine_shape():
    Quarantine.model_validate({"experiment_id": "exp-boutique-pay-089",
                               "quarantined_at": "2026-10-11T00:50:38Z", "reason": "abort"})


def test_unknown_fields_are_rejected():
    data = fixture("CanaryVerdict.json")
    data["extra"] = 1
    with pytest.raises(ValidationError):
        ARTIFACT_MODELS["CanaryVerdict.json"].model_validate(data)


def test_plan_must_leave_one_replica_untouched():
    data = fixture("ExperimentPlan.json")
    data["target_node"]["targetable_replicas"] = 2
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)
    data["target_node"].update(replicas_total=1, targetable_replicas=1)
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)


def test_latency_threshold_must_exceed_injected_delay():
    data = fixture("ExperimentPlan.json")
    data["abort_thresholds"]["max_p99_latency_ms"] = 270.0  # == delay 250 + jitter 20
    with pytest.raises(ValidationError):
        ExperimentPlan.model_validate(data)


def test_naive_timestamps_are_rejected():
    data = fixture("InjectionReceipt.json")
    data["timestamps"]["initiated_at"] = "2026-10-11T00:50:15"
    with pytest.raises(ValidationError):
        InjectionReceipt.model_validate(data)


def test_info_recommendation_has_no_diff():
    data = fixture("Recommendation.json")
    data["priority"] = "INFO"
    with pytest.raises(ValidationError):
        Recommendation.model_validate(data)
    data.update(suggested_diff=None, target_file=None, target_symbol=None)
    Recommendation.model_validate(data)


def test_rca_steady_state_allows_empty_cascade():
    data = fixture("RootCauseAnalysis.json")
    data.update(verdict="STEADY_STATE_MAINTAINED", cascade_path=[], root_cause_isolation=None)
    data["impact_summary"]["steady_state_maintained"] = True
    RootCauseAnalysis.model_validate(data)


def test_io_round_trip(tmp_path):
    assert read_artifact(tmp_path, "Recommendation.json") is None
    rec = Recommendation.model_validate(fixture("Recommendation.json"))
    path = write_artifact(tmp_path, rec)
    assert path.name == "Recommendation.json"
    assert "$schema" in json.loads(path.read_text(encoding="utf-8"))
    assert read_artifact(tmp_path, "Recommendation.json") == rec
    assert not list(tmp_path.glob("*.tmp"))


def test_write_model_validates_before_writing(tmp_path):
    rec = Recommendation.model_validate(fixture("Recommendation.json"))
    rec = rec.model_copy(update={"priority": Priority.INFO})  # model_copy skips validation
    with pytest.raises(ValidationError):
        write_model(tmp_path / "Recommendation.json", rec)
    assert not (tmp_path / "Recommendation.json").exists()


def test_mock_pipeline_end_to_end(tmp_path):
    result = subprocess.run(
        [sys.executable, "run_pipeline.py", "--mock", "--artifacts", str(tmp_path)],
        cwd=CHAOS_ROOT, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    exp_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(exp_dirs) == 1
    for name in ("ExperimentPlan.json", "InjectionReceipt.json", "RootCauseAnalysis.json", "Recommendation.json"):
        assert read_artifact(exp_dirs[0], name) is not None, name
    assert read_artifact(exp_dirs[0], "InjectionReceipt.json").is_final
