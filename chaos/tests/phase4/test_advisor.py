"""Phase 4 advisor: localization, gap audit, patch synthesis and the Recommendation contract."""
from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from common.config import CHAOS_ROOT, FIXTURES_DIR, REPO_ROOT
from common.contracts import ExperimentPlan, Priority, Recommendation, RootCauseAnalysis
from common.io import read_artifact, read_json, write_artifact
from phase4_advisor.advisor import build_recommendation, choose_deadline_ms
from phase4_advisor.localize import localize
from phase4_advisor.patches import git_apply_check, python_deadline


def plan_fixture() -> ExperimentPlan:
    return ExperimentPlan.model_validate(read_json(FIXTURES_DIR / "ExperimentPlan.json"))


def rca_fixture(**changes) -> RootCauseAnalysis:
    data = copy.deepcopy(read_json(FIXTURES_DIR / "RootCauseAnalysis.json"))
    data.update(changes)
    return RootCauseAnalysis.model_validate(data)


def make_exp(tmp_path, rca: RootCauseAnalysis):
    exp = tmp_path / "exp-boutique-pay-089"
    write_artifact(exp, plan_fixture())
    write_artifact(exp, rca)
    return exp


def run_advisor(exp, *extra):
    return subprocess.run([sys.executable, "-m", "phase4_advisor", "--exp", str(exp), "--repo", str(REPO_ROOT),
                           "--mock", *extra], cwd=CHAOS_ROOT, capture_output=True, text=True, timeout=120)


def test_abort_produces_high_priority_applicable_patch(tmp_path):
    exp = make_exp(tmp_path, rca_fixture())
    result = run_advisor(exp)
    assert result.returncode == 0, result.stderr
    rec = read_artifact(exp, "Recommendation.json")
    assert rec.priority == Priority.HIGH
    assert rec.target_file == "src/checkoutservice/main.go"
    assert rec.target_symbol == "chargeCard()"
    assert rec.vulnerability_type == "CASCADING_TIMEOUT_COLLAPSE"
    assert "context.WithTimeout(ctx, 150*time.Millisecond)" in rec.suggested_diff
    assert "Charge(tCtx," in rec.suggested_diff
    assert git_apply_check(REPO_ROOT, rec.suggested_diff)[0]
    assert (exp / "ResilienceAudit.json").exists()
    assert (exp / "Recommendation.patch").read_text(encoding="utf-8") == rec.suggested_diff


def test_advisor_never_modifies_src(tmp_path):
    before = subprocess.run(["git", "status", "--porcelain", "--", "src", "istio-manifests"], cwd=REPO_ROOT,
                            capture_output=True, text=True).stdout
    run_advisor(make_exp(tmp_path, rca_fixture()))
    after = subprocess.run(["git", "status", "--porcelain", "--", "src", "istio-manifests"], cwd=REPO_ROOT,
                           capture_output=True, text=True).stdout
    assert before == after


def test_gap_audit_records_each_control(tmp_path):
    rec, audit = build_recommendation(tmp_path, REPO_ROOT, plan_fixture(), rca_fixture(), use_llm=False)
    controls = {c["name"]: c for c in audit["controls"]}
    assert set(controls) == {"timeout", "retry", "circuit_breaker", "fallback"}
    assert not any(c["present"] for c in controls.values())  # stock Online Boutique has none of them
    assert audit["patch"]["mesh_patch"] == "istio-manifests/paymentservice-resilience.yaml"
    assert "outlierDetection" in rec.suggested_diff and "retries:" in rec.suggested_diff


def test_line_numbers_in_rca_are_not_trusted(tmp_path):
    iso = {"faulty_service": "checkoutservice", "mechanism": "blocked on Charge",
           "stack_trace_snippet": "at checkoutservice/main.go:9999"}
    rec, _ = build_recommendation(tmp_path, REPO_ROOT, plan_fixture(), rca_fixture(root_cause_isolation=iso),
                                  use_llm=False)
    assert rec.target_symbol == "chargeCard()"


def test_steady_state_is_info_without_diff(tmp_path):
    rca = rca_fixture(verdict="STEADY_STATE_MAINTAINED", cascade_path=[], root_cause_isolation=None,
                      impact_summary={"steady_state_maintained": True, "lowest_checkout_success_rate": 99.97,
                                      "peak_p99_latency_ms": 21.0})
    exp = make_exp(tmp_path, rca)
    assert run_advisor(exp).returncode == 0
    rec = read_artifact(exp, "Recommendation.json")
    assert rec.priority == Priority.INFO
    assert rec.suggested_diff is None
    assert "tolerated" in rec.summary
    assert not (exp / "Recommendation.patch").exists()


def test_degraded_without_abort_is_medium(tmp_path):
    rec, _ = build_recommendation(tmp_path, REPO_ROOT, plan_fixture(), rca_fixture(verdict="DEGRADED_NO_ABORT"),
                                  use_llm=False)
    assert rec.priority == Priority.MEDIUM
    Recommendation.model_validate(rec.model_dump(by_alias=True))


def test_unknown_service_is_reported_not_guessed(tmp_path):
    iso = {"faulty_service": "ledgerservice", "mechanism": "unknown"}
    rec, audit = build_recommendation(tmp_path, REPO_ROOT, plan_fixture(),
                                      rca_fixture(root_cause_isolation=iso, cascade_path=[]), use_llm=False)
    assert rec.vulnerability_type == "UNLOCALIZED_DEPENDENCY_FAILURE"
    assert rec.target_file is None and rec.suggested_diff is None
    assert audit["localization"]["notes"]


def test_llm_unavailable_falls_back_to_templates(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "")
    _, audit = build_recommendation(tmp_path, REPO_ROOT, plan_fixture(), rca_fixture(), use_llm=True)
    assert audit["patch"]["source"] == "template"


def test_missing_rca_fails(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    write_artifact(exp, plan_fixture())
    result = run_advisor(exp)
    assert result.returncode == 1
    assert "RootCauseAnalysis.json" in result.stderr
    assert not (exp / "Recommendation.json").exists()


def test_multiline_go_call_and_request_context():
    site = localize(REPO_ROOT, "frontend", "checkoutservice").primary
    assert site.symbol == "placeOrderHandler()" and site.method == "PlaceOrder"
    assert site.ctx_arg == "r.Context()"


def test_python_call_site_gets_timeout_kwarg():
    site = localize(REPO_ROOT, "recommendationservice", "productcatalogservice").primary
    assert site.language == "python" and site.method == "ListProducts"
    diff = python_deadline(REPO_ROOT, site, 150)
    assert "ListProducts(demo_pb2.Empty(), timeout=0.15)" in diff
    assert git_apply_check(REPO_ROOT, diff)[0]


@pytest.mark.parametrize("p99,limit,expected", [(18.5, 500.0, 150), (2.0, 500.0, 100), (120.0, 300.0, 150)])
def test_deadline_budget(p99, limit, expected):
    plan = plan_fixture().model_copy(deep=True)
    plan.steady_state_baseline.p99_latency_ms = p99
    plan.abort_thresholds.max_p99_latency_ms = limit
    assert choose_deadline_ms(plan) == expected
