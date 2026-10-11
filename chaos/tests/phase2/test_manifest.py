"""Phase 2: Chaos Mesh manifest synthesis from an ExperimentPlan."""
from __future__ import annotations

import copy

import pytest

from common.config import FIXTURES_DIR
from common.contracts import ExperimentPlan
from common.io import read_json
from phase2_injection.manifest import (
    LABEL_KEY, UnsupportedFault, build_manifest, describe_fault, dump_manifest, manifest_filename,
)


def plan(**overrides) -> ExperimentPlan:
    data = copy.deepcopy(read_json(FIXTURES_DIR / "ExperimentPlan.json"))
    fp = overrides.pop("fault_profile", None)
    if fp:
        data["fault_profile"].update(fp)
    data.update(overrides)
    return ExperimentPlan.model_validate(data)


def test_latency_manifest_matches_the_contract():
    kind, m = build_manifest(plan(), duration_seconds=60, target_pods=1)
    assert kind == "NetworkChaos"
    assert m["apiVersion"] == "chaos-mesh.org/v1alpha1"
    assert m["metadata"]["labels"][LABEL_KEY] == "exp-boutique-pay-089"
    assert m["metadata"]["namespace"] == "default"
    spec = m["spec"]
    assert spec["action"] == "delay"
    assert spec["mode"] == "fixed"
    assert spec["value"] == "1"
    assert spec["selector"] == {"namespaces": ["default"], "labelSelectors": {"app": "paymentservice"}}
    assert spec["delay"]["latency"] == "250ms"
    assert spec["delay"]["jitter"] == "20ms"
    assert spec["duration"] == "60s"
    # No caller named in the plan -> unscoped (applies to all of the pod's traffic).
    assert "target" not in spec
    assert manifest_filename("exp-boutique-pay-089", kind) == "exp-boutique-pay-089-net.yaml"


def test_caller_scopes_the_fault_to_one_upstream():
    p = plan(fault_profile={"parameters": {"delay_ms": 250, "jitter_ms": 20,
                                           "target_pods": 1, "upstream_caller": "checkoutservice"}})
    _, m = build_manifest(p, duration_seconds=30, target_pods=1)
    spec = m["spec"]
    assert spec["direction"] == "to"
    assert spec["target"]["selector"]["labelSelectors"]["app"] == "checkoutservice"


def test_latency_threshold_validator_still_guards_the_plan():
    # delay 250 + jitter 20 == 270, threshold must exceed it.
    with pytest.raises(Exception):
        plan(abort_thresholds={"max_p99_latency_ms": 270.0,
                               "max_error_rate_percent": 0.5,
                               "min_checkout_success_rate_percent": 99.5})


def test_loss_mapping():
    p = plan(fault_profile={"category": "network", "action": "loss",
                            "parameters": {"loss_percent": 40, "target_pods": 1}})
    kind, m = build_manifest(p, duration_seconds=60, target_pods=1)
    assert kind == "NetworkChaos"
    assert m["spec"]["action"] == "loss"
    assert m["spec"]["loss"]["loss"] == "40"


def test_pod_kill_mapping_has_no_duration():
    p = plan(fault_profile={"category": "pod", "action": "pod-kill", "parameters": {"target_pods": 1}})
    kind, m = build_manifest(p, duration_seconds=60, target_pods=1)
    assert kind == "PodChaos"
    assert m["spec"]["action"] == "pod-kill"
    assert "duration" not in m["spec"]  # one-shot, nothing to roll back


def test_cpu_and_memory_stress_mapping():
    cpu = plan(fault_profile={"category": "stress", "action": "cpu",
                              "parameters": {"workers": 2, "load": 90, "target_pods": 1}})
    kind, m = build_manifest(cpu, duration_seconds=60, target_pods=1)
    assert kind == "StressChaos"
    assert m["spec"]["stressors"]["cpu"] == {"workers": 2, "load": 90}

    mem = plan(fault_profile={"category": "stress", "action": "memory",
                              "parameters": {"workers": 1, "size": "512MB", "target_pods": 1}})
    _, m2 = build_manifest(mem, duration_seconds=60, target_pods=1)
    assert m2["spec"]["stressors"]["memory"]["size"] == "512MB"


def test_memory_stress_accepts_numeric_size_mb():
    # Phase 1 and the console carry a numeric size_mb; the manifest renders it as a Chaos Mesh size string.
    mem = plan(fault_profile={"category": "stress", "action": "memory",
                              "parameters": {"workers": 2, "size_mb": 384, "target_pods": 1}})
    kind, m = build_manifest(mem, duration_seconds=60, target_pods=1)
    assert kind == "StressChaos"
    assert m["spec"]["stressors"]["memory"] == {"workers": 2, "size": "384MB"}


def test_dependency_failure_partitions_the_named_caller():
    dep = plan(fault_profile={"category": "network", "action": "dependency",
                              "parameters": {"caller": "checkoutservice", "direction": "both", "target_pods": 1}})
    kind, m = build_manifest(dep, duration_seconds=45, target_pods=1)
    assert kind == "NetworkChaos"
    assert m["spec"]["action"] == "partition"
    assert m["spec"]["direction"] == "both"
    assert m["spec"]["target"]["selector"]["labelSelectors"]["app"] == "checkoutservice"
    assert m["spec"]["duration"] == "45s"


def test_unsupported_fault_raises():
    p = plan(fault_profile={"category": "chaos", "action": "banana", "parameters": {}})
    with pytest.raises(UnsupportedFault):
        build_manifest(p, duration_seconds=60, target_pods=1)


def test_describe_fault_is_human_readable():
    kind, m = build_manifest(plan(), duration_seconds=60, target_pods=1)
    text = describe_fault(kind, m)
    assert "NetworkChaos" in text and "latency=250ms" in text and "jitter=20ms" in text


def test_dump_manifest_round_trips():
    _, m = build_manifest(plan(), duration_seconds=60, target_pods=1)
    text = dump_manifest(m)
    # Valid as JSON (the fallback) or YAML; either way the key fields survive.
    assert "NetworkChaos" in text and "250ms" in text
