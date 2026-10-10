"""Phase 1: Observation and Dynamic Profiling (Red Agent).

Discovers cluster topology, profiles rolling steady-state golden signals,
computes the blast-radius budget, selects the highest-impact redundant target,
and emits a validated ExperimentPlan.json and topology.json.

CLI:
  python -m phase1_observation --out <ARTIFACTS_DIR> [--mock]
      [--target SERVICE] [--fault latency|loss|pod-kill] [--delay-ms N] [--jitter-ms N]
      [--loss-percent N] [--duration SECONDS] [--max-p99-ms N]

Without --target / --fault it picks the highest-impact target and a 250 ms latency fault itself.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from common.config import get_settings
from common.contracts import AbortThresholds

from .blast_radius import compute_blast_radius_budget
from .plan_builder import build_and_save_experiment_plan
from .profiler import ProfilingError, profile_steady_state
from .target_selector import FAULTS, describe_fault, select_target_and_fault
from .topology import TopologyError, build_topology

logger = logging.getLogger("phase1_observation")


def run_observation(
    artifacts_dir: Path | str,
    mock: bool = False,
    window_minutes: int = 15,
    namespace: str | None = None,
    target: str | None = None,
    fault: str | None = None,
    fault_params: dict | None = None,
    duration_seconds: int = 60,
    max_p99_ms: float | None = None,
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
    #    An operator may name the target and the fault; the replica rule still applies.
    target_selection = select_target_and_fault(
        topology=topology,
        category="network",
        action=fault or "latency",
        delay_ms=250,
        jitter_ms=20,
        max_duration_seconds=duration_seconds,
        target=target,
        parameters=(fault_params or {}) if fault else None,
    )

    # 4. Blast-Radius Budget and Abort Thresholds
    budget = compute_blast_radius_budget(
        baseline=profiling.steady_state_baseline,
        fault_profile=target_selection.fault_profile,
        slo_checkout_success_percent=settings.slo_checkout_success_percent,
        safety_alpha=settings.safety_alpha,
    )

    # An operator may tighten (or loosen) the p99 abort limit; the contract still requires it to exceed
    # delay + jitter for a latency fault.
    if max_p99_ms is not None:
        injected = 0.0
        if target_selection.fault_profile.action == "latency":
            fp = target_selection.fault_profile.parameters
            injected = float(fp.get("delay_ms", 0)) + float(fp.get("jitter_ms", 0))
        if max_p99_ms <= injected:
            raise ValueError(f"--max-p99-ms {max_p99_ms:g} must be above the injected {injected:g} ms (delay + jitter)")
        th = budget.abort_thresholds
        budget = dataclasses.replace(budget, abort_thresholds=AbortThresholds(
            max_p99_latency_ms=float(max_p99_ms), max_error_rate_percent=th.max_error_rate_percent,
            min_checkout_success_rate_percent=th.min_checkout_success_rate_percent))

    # 5. Assemble, Validate and Write Artifacts
    plan, exp_dir = build_and_save_experiment_plan(
        artifacts_dir=out,
        topology=topology,
        profiling=profiling,
        target_selection=target_selection,
        budget=budget,
    )

    print(f"[phase1] Target selected: {plan.target_node.service_name} ({plan.target_node.targetable_replicas}/{plan.target_node.replicas_total} replicas)")
    print(f"[phase1] Fault profile: {describe_fault(plan.fault_profile)} for {plan.fault_profile.max_duration_seconds}s (ramp: {plan.fault_profile.ramp_strategy})")
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
    parser.add_argument("--target", default=None, help="service to inject into (default: the highest-impact one)")
    parser.add_argument("--fault", choices=sorted(FAULTS), default=None, help="fault to inject (default: latency)")
    parser.add_argument("--delay-ms", type=int, default=None, help="latency: added delay")
    parser.add_argument("--jitter-ms", type=int, default=None, help="latency: jitter")
    parser.add_argument("--loss-percent", type=int, default=None, help="loss: share of packets dropped")
    parser.add_argument("--duration", type=int, default=60, help="fault window in seconds (default: 60)")
    parser.add_argument("--max-p99-ms", type=float, default=None,
                        help="abort if frontend p99 exceeds this (default: derived from the baseline)")
    args = parser.parse_args(argv)
    if not 10 <= args.duration <= 600:
        parser.error("--duration must be between 10 and 600 seconds")
    params = {"delay_ms": args.delay_ms, "jitter_ms": args.jitter_ms, "loss_percent": args.loss_percent}
    params = {k: v for k, v in params.items() if v is not None}
    if params and not args.fault:
        args.fault = "latency" if "loss_percent" not in params else "loss"

    settings = get_settings()
    out = args.out or settings.artifacts_dir

    try:
        run_observation(
            artifacts_dir=out,
            mock=args.mock,
            window_minutes=args.window,
            namespace=args.namespace,
            target=args.target,
            fault=args.fault,
            fault_params=params,
            duration_seconds=args.duration,
            max_p99_ms=args.max_p99_ms,
        )
        return 0
    except (ValueError, ProfilingError, TopologyError) as e:  # a request or a measurement problem, not a bug
        print(f"[phase1 error] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        logger.error("Phase 1 failed: %s", e, exc_info=True)
        print(f"[phase1 error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
