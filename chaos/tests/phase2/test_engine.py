"""Phase 2: the injection engine handshake, in mock mode (no cluster).

Each test pre-writes the Blue Agent's artifacts (CanaryVerdict / AbortSignal)
into the experiment folder so the handshake resolves deterministically without
Phase 3 running.
"""
from __future__ import annotations

import copy
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from common.config import FIXTURES_DIR, get_settings
from common.contracts import (
    AbortSignal, CanaryDecision, CanaryVerdict, ExperimentPlan, InjectionStatus, Quarantine,
)
from common.io import read_artifact, read_json, write_artifact, write_quarantine
from phase2_injection.engine import run
from phase2_injection.kube import MockInjector


def _settings(**overrides):
    return dataclasses.replace(get_settings(), **overrides)


def _fast_settings():
    # Canary watch instant; verdict already on disk so the wait returns at once.
    return _settings(canary_seconds=0, canary_verdict_timeout_seconds=3)


def make_plan(exp_dir: Path, **fault_overrides) -> ExperimentPlan:
    data = copy.deepcopy(read_json(FIXTURES_DIR / "ExperimentPlan.json"))
    if fault_overrides:
        data["fault_profile"].update(fault_overrides)
    plan = ExperimentPlan.model_validate(data)
    write_artifact(exp_dir, plan)
    return plan


def put_verdict(exp_dir: Path, plan: ExperimentPlan, decision: CanaryDecision) -> None:
    write_artifact(exp_dir, CanaryVerdict(experiment_id=plan.experiment_id, decision=decision,
                                          checked_at=datetime.now(timezone.utc), reason="test"))


def put_abort(exp_dir: Path, plan: ExperimentPlan) -> None:
    write_artifact(exp_dir, AbortSignal(experiment_id=plan.experiment_id,
                                        aborted_at=datetime.now(timezone.utc),
                                        breached_metric="min_checkout_success_rate_percent",
                                        observed_value=99.38, threshold=99.5))


def run_mock(exp_dir: Path, plan: ExperimentPlan):
    injector = MockInjector(plan.target_node.replicas_total)
    code = run(exp_dir, mock=True, settings=_fast_settings(), injector=injector, log=lambda *_: None)
    receipt = read_artifact(exp_dir, "InjectionReceipt.json")
    return code, receipt, injector


# ---------------------------------------------------------------- main paths

def test_go_completed(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)
    put_verdict(exp, plan, CanaryDecision.GO)

    code, receipt, injector = run_mock(exp, plan)

    assert code == 0
    assert receipt.status == InjectionStatus.COMPLETED
    assert receipt.is_final
    assert receipt.timestamps.ended_at is not None
    assert receipt.execution_details.target_pod == "paymentservice-mock-0"
    assert receipt.execution_details.pod_scope == "1 of 2 replicas"
    assert "NetworkChaos delay" in receipt.execution_details.fault_applied
    # Manifest written, fault applied, and torn down in cleanup.
    assert (exp / "exp-boutique-pay-089-net.yaml").exists()
    assert injector.applied and injector.deleted


def test_go_then_abort(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)
    put_verdict(exp, plan, CanaryDecision.GO)
    put_abort(exp, plan)

    code, receipt, _ = run_mock(exp, plan)

    assert code == 0
    assert receipt.status == InjectionStatus.ABORTED
    assert receipt.is_final


def test_no_go_verdict(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)
    put_verdict(exp, plan, CanaryDecision.NO_GO)

    code, receipt, _ = run_mock(exp, plan)

    assert code == 0
    assert receipt.status == InjectionStatus.NO_GO
    assert receipt.is_final


def test_no_verdict_times_out_to_no_go(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)  # no CanaryVerdict written

    injector = MockInjector(plan.target_node.replicas_total)
    code = run(exp, mock=True, settings=_settings(canary_seconds=0, canary_verdict_timeout_seconds=0),
               injector=injector, log=lambda *_: None)
    receipt = read_artifact(exp, "InjectionReceipt.json")

    assert code == 0
    assert receipt.status == InjectionStatus.NO_GO


# ---------------------------------------------------------------- failure paths

def test_quarantine_refuses_to_run(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)
    write_quarantine(exp, Quarantine(experiment_id=plan.experiment_id,
                                     quarantined_at=datetime.now(timezone.utc), reason="prior abort"))

    code = run(exp, mock=True, settings=_fast_settings(), injector=MockInjector(), log=lambda *_: None)

    assert code == 2
    assert read_artifact(exp, "InjectionReceipt.json") is None  # it never ran


def test_unsupported_fault_fails_cleanly(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp, category="chaos", action="banana", parameters={})

    code, receipt, injector = run_mock(exp, plan)

    assert code == 1
    assert receipt.status == InjectionStatus.FAILED
    assert receipt.is_final
    assert not injector.applied  # nothing was injected


def test_missing_replicas_fails_preflight(tmp_path):
    exp = tmp_path / "exp-boutique-pay-089"
    plan = make_plan(exp)
    # Only one ready replica, but the plan expects two.
    injector = MockInjector(replicas_total=1)
    code = run(exp, mock=True, settings=_fast_settings(), injector=injector, log=lambda *_: None)
    receipt = read_artifact(exp, "InjectionReceipt.json")

    assert code == 1
    assert receipt.status == InjectionStatus.FAILED
    assert not injector.applied
