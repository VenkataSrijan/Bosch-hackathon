"""ExperimentPlan assembly, experiment ID generation, and artifact writing."""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common.contracts import ExperimentPlan
from common.io import write_artifact

from .blast_radius import BlastRadiusBudget
from .profiler import ProfilingResult
from .target_selector import TargetSelection
from .topology import TopologyGraph, save_topology

logger = logging.getLogger(__name__)


def generate_experiment_id(target_service: str = "paymentservice", app_name: str = "boutique") -> str:
    """Generates a fresh experiment ID matching r'^exp-[a-z0-9]+-[a-z0-9-]+-\\d{3}$'."""
    # Canonical short abbreviation or clean service slug
    short_target = target_service.lower().replace("service", "")
    if not short_target:
        short_target = target_service.lower()[:8]
    number = f"{random.randint(1, 999):03d}"
    return f"exp-{app_name}-{short_target}-{number}"


def build_and_save_experiment_plan(
    artifacts_dir: Path | str,
    topology: TopologyGraph,
    profiling: ProfilingResult,
    target_selection: TargetSelection,
    budget: BlastRadiusBudget,
    experiment_id: Optional[str] = None,
) -> tuple[ExperimentPlan, Path]:
    """Assembles the validated ExperimentPlan, writes artifacts atomically, and returns the path."""
    exp_id = experiment_id or generate_experiment_id(
        target_service=target_selection.target_node.service_name,
        app_name="boutique",
    )

    exp_dir = Path(artifacts_dir) / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    plan = ExperimentPlan(
        experiment_id=exp_id,
        created_at=datetime.now(timezone.utc),
        target_node=target_selection.target_node,
        fault_profile=target_selection.fault_profile,
        steady_state_baseline=profiling.steady_state_baseline,
        abort_thresholds=budget.abort_thresholds,
    )

    # 1. Write ExperimentPlan.json atomically
    plan_path = write_artifact(exp_dir, plan)

    # 2. Save topology.json
    save_topology(exp_dir, topology)

    # 3. Save internal observation metadata for auditing
    meta_path = exp_dir / "observation_metadata.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "hypothesis": target_selection.hypothesis,
                "ranking_score": target_selection.ranking_score,
                "candidate_summary": target_selection.candidate_summary,
                "blast_radius_budget": {
                    "baseline_error_rate": budget.baseline_error_rate,
                    "slo_error_budget": budget.slo_error_budget,
                    "remaining_error_budget": budget.remaining_error_budget,
                    "safety_alpha": budget.safety_alpha,
                    "max_allowable_degradation": budget.max_allowable_degradation,
                },
                "service_profiles": profiling.to_dict()["service_profiles"],
            },
            fh,
            indent=2,
        )
        fh.write("\n")

    logger.info("Phase 1: ExperimentPlan generated at %s", plan_path)
    return plan, exp_dir
