"""Phase 3 in the mock pipeline: the abort scenario breaches after a GO canary; healthy stays steady."""
from __future__ import annotations

import subprocess
import sys

from common.config import CHAOS_ROOT, FIXTURES_DIR
from common.contracts import ExperimentPlan
from common.io import quarantine_path, read_artifact, read_json, write_artifact


def test_pipeline_abort_scenario(tmp_path):
    result = subprocess.run([sys.executable, "run_pipeline.py", "--mock", "--artifacts", str(tmp_path)],
                            cwd=CHAOS_ROOT, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    exp = next(p for p in tmp_path.iterdir() if p.is_dir())

    verdict = read_artifact(exp, "CanaryVerdict.json")
    signal = read_artifact(exp, "AbortSignal.json")
    assert verdict.decision == "GO"
    assert signal.aborted_at > verdict.checked_at  # the breach comes after the GO
    assert read_artifact(exp, "InjectionReceipt.json").status == "ABORTED"
    assert quarantine_path(exp).exists()

    rca = read_artifact(exp, "RootCauseAnalysis.json")
    assert rca.verdict == "ABORT_TRIGGERED"
    assert [h.service for h in rca.cascade_path] == ["paymentservice", "checkoutservice", "frontend"]
    assert rca.root_cause_isolation.faulty_service == "checkoutservice"
    assert rca.impact_summary.peak_p99_latency_ms >= signal.observed_value
    assert read_json(exp / "JudgeLog.json")["missing_samples"]  # the recorded gap is logged, not filled in


def test_healthy_scenario(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    write_artifact(exp, ExperimentPlan.model_validate(read_json(FIXTURES_DIR / "ExperimentPlan.json")))
    judge = subprocess.Popen([sys.executable, "-m", "phase3_judge", "--exp", str(exp), "--mock",
                              "--mock-scenario", "healthy"], cwd=CHAOS_ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    red = subprocess.run([sys.executable, "-m", "phase2_injection", "--exp", str(exp), "--mock"],
                         cwd=CHAOS_ROOT, capture_output=True, text=True, timeout=120)
    out, _ = judge.communicate(timeout=120)
    assert red.returncode == 0 and judge.returncode == 0, red.stdout + red.stderr + out

    assert read_artifact(exp, "CanaryVerdict.json").decision == "GO"
    assert read_artifact(exp, "InjectionReceipt.json").status == "COMPLETED"
    assert not (exp / "AbortSignal.json").exists() and not quarantine_path(exp).exists()
    rca = read_artifact(exp, "RootCauseAnalysis.json")
    assert rca.verdict == "STEADY_STATE_MAINTAINED"
    assert rca.cascade_path == [] and rca.root_cause_isolation is None
