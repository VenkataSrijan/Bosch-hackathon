"""Phase 1: Observation and Dynamic Profiling (Red Agent).

Discovers cluster topology, profiles rolling steady-state golden signals,
computes the blast-radius budget, selects the highest-impact redundant target,
and emits a validated ExperimentPlan.json and topology.json.

CLI:
  python -m phase1_observation --out <ARTIFACTS_DIR> [--mock]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from common.config import get_settings

from .blast_radius import compute_blast_radius_budget
from .plan_builder import build_and_save_experiment_plan
from .profiler import profile_steady_state
from .target_selector import select_target_and_fault
from .topology import build_topology

logger = logging.getLogger("phase1_observation")


def run_observation(
    artifacts_dir: Path | str,
    mock: bool = False,
    window_minutes: int = 15,
    namespace: str | None = None,
) -> Path:
    """Executes the full Phase 1 observation and profiling workflow."""
    settings = get_settings()
    out = Path(artifacts_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    ns = namespace or settings.kube_namespace

    # 1. Topology Mapping (Kubernetes + Prometheus + Jaeger)
    topology = build_topology(
        kube_namespace=ns,
        prometheus_url=settings.prometheus_url,
        jaeger_url=settings.jaeger_url,
        mock=mock,
    )

    # 2. Steady-State Profiling over rolling window
    profiling = profile_steady_state(
        prometheus_url=settings.prometheus_url,
        window_minutes=window_minutes,
        kube_namespace=ns,
        mock=mock,
    )

    # 3. Target and Fault Selection (Centrality, Fan-in, >= 2 replicas, LLM hypothesis)
    target_selection = select_target_and_fault(
        topology=topology,
        category="network",
        action="latency",
        delay_ms=250,
        jitter_ms=20,
        max_duration_seconds=60,
    )

    # 4. Blast-Radius Budget and Abort Thresholds
    budget = compute_blast_radius_budget(
        baseline=profiling.steady_state_baseline,
        fault_profile=target_selection.fault_profile,
        slo_checkout_success_percent=settings.slo_checkout_success_percent,
        safety_alpha=settings.safety_alpha,
    )

    # 5. Assemble, Validate and Write Artifacts
    plan, exp_dir = build_and_save_experiment_plan(
        artifacts_dir=out,
        topology=topology,
        profiling=profiling,
        target_selection=target_selection,
        budget=budget,
    )

    print(f"[phase1] Target selected: {plan.target_node.service_name} ({plan.target_node.targetable_replicas}/{plan.target_node.replicas_total} replicas)")
    print(f"[phase1] Fault profile: {plan.fault_profile.action} {plan.fault_profile.parameters.get('delay_ms')}ms (ramp: {plan.fault_profile.ramp_strategy})")
    print(f"[phase1] Abort boundary: max_p99={plan.abort_thresholds.max_p99_latency_ms}ms, min_success={plan.abort_thresholds.min_checkout_success_rate_percent}%")
    print(f"[phase1] Wrote {plan.experiment_id}/ExperimentPlan.json and topology.json")

    # MUST be the last line of stdout
    print(str(exp_dir))
    return exp_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="phase1_observation",
        description="Phase 1: Observation and Dynamic Profiling",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="artifacts root directory (default: ARTIFACTS_DIR)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="run in mock mode using recorded cluster telemetry",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=15,
        help="steady-state rolling profiling window in minutes (default: 15)",
    )
    parser.add_argument(
        "--namespace",
        type=str,
        default=None,
        help="target kubernetes namespace (default: KUBE_NAMESPACE)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    out = args.out or settings.artifacts_dir

    try:
        run_observation(
            artifacts_dir=out,
            mock=args.mock,
            window_minutes=args.window,
            namespace=args.namespace,
        )
        return 0
    except Exception as e:
        logger.error("Phase 1 failed: %s", e, exc_info=True)
        print(f"[phase1 error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
