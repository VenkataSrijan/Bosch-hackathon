"""Unit and integration tests for Phase 1: Observation and Dynamic Profiling."""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from common.contracts import ExperimentPlan
from common.io import EXPERIMENT_PLAN, read_model
from phase1_observation.__main__ import main, run_observation
from phase1_observation.blast_radius import compute_blast_radius_budget
from phase1_observation.profiler import profile_steady_state
from phase1_observation.target_selector import select_target_and_fault
from phase1_observation.topology import NodeCapacity, ServiceNode, TopologyGraph, build_topology


def test_phase1_main_path_mock(tmp_path: Path) -> None:
    """Test full Phase 1 observation pipeline in mock mode."""
    exp_dir = run_observation(artifacts_dir=tmp_path, mock=True)

    assert exp_dir.exists()
    plan_file = exp_dir / EXPERIMENT_PLAN
    assert plan_file.exists()

    # Must validate against Pydantic model contract
    plan = read_model(plan_file, ExperimentPlan)
    assert plan.target_node.service_name == "paymentservice"
    assert plan.target_node.replicas_total >= 2
    assert plan.target_node.targetable_replicas <= plan.target_node.replicas_total - 1

    # Latency abort threshold must strictly exceed delay + jitter
    delay = float(plan.fault_profile.parameters.get("delay_ms", 0))
    jitter = float(plan.fault_profile.parameters.get("jitter_ms", 0))
    assert plan.abort_thresholds.max_p99_latency_ms > delay + jitter

    # Verify topology.json was written
    topo_file = exp_dir / "topology.json"
    assert topo_file.exists()
    with open(topo_file) as f:
        topo = json.load(f)
    assert "nodes" in topo
    assert "edges" in topo
    assert len(topo["nodes"]) >= 5


def test_phase1_cli_last_line_is_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test CLI execution and verify that the last line of stdout is the experiment directory."""
    stdout_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout_buf)

    code = main(["--out", str(tmp_path), "--mock"])
    assert code == 0

    output = stdout_buf.getvalue().strip()
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    assert len(lines) >= 2

    # The last line MUST be the valid created experiment directory path
    last_line = lines[-1]
    last_path = Path(last_line)
    assert last_path.exists()
    assert (last_path / EXPERIMENT_PLAN).exists()


def test_target_selection_requires_at_least_two_replicas() -> None:
    """Failure path: if all services have 1 replica, target selection MUST raise ValueError."""
    dummy_nodes = {
        "svc-a": ServiceNode(
            name="svc-a",
            namespace="default",
            deployment="svc-a",
            capacity=NodeCapacity(replicas_total=1, available_replicas=1),
        ),
        "svc-b": ServiceNode(
            name="svc-b",
            namespace="default",
            deployment="svc-b",
            capacity=NodeCapacity(replicas_total=1, available_replicas=1),
        ),
    }
    topo = TopologyGraph(nodes=dummy_nodes)

    with pytest.raises(ValueError, match="No service has at least 2 replicas"):
        select_target_and_fault(topo)


def test_blast_radius_budget_math() -> None:
    """Verify mathematical calculation of blast radius and abort thresholds."""
    topo = build_topology(mock=True)
    profiling = profile_steady_state(mock=True)
    target_sel = select_target_and_fault(topo)

    budget = compute_blast_radius_budget(
        baseline=profiling.steady_state_baseline,
        fault_profile=target_sel.fault_profile,
        slo_checkout_success_percent=99.5,
        safety_alpha=0.10,
    )

    # SLO budget is 100 - 99.5 = 0.50%
    assert budget.slo_error_budget == 0.50
    # Remaining budget = 0.50 - baseline error rate (0.02) = 0.48%
    assert pytest.approx(budget.remaining_error_budget, 0.001) == 0.48
    # Max allowable degradation = baseline (0.02) + remaining * 0.10 = 0.068
    assert pytest.approx(budget.max_allowable_degradation, 0.001) == 0.068

    # Latency threshold must strictly exceed delay + jitter
    injected = target_sel.fault_profile.parameters["delay_ms"] + target_sel.fault_profile.parameters["jitter_ms"]
    assert budget.abort_thresholds.max_p99_latency_ms > injected


def test_topology_graph_edge_and_degree_calculations() -> None:
    """Verify directed dependency graph in-degree and out-degree properties."""
    topo = build_topology(mock=True)
    assert "paymentservice" in topo.nodes
    assert "checkoutservice" in topo.nodes

    payment_node = topo.nodes["paymentservice"]
    assert "checkoutservice" in payment_node.callers
    assert payment_node.in_degree >= 1
