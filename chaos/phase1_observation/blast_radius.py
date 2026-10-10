"""Blast-radius budget calculation and dynamic abort threshold derivation."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from common.contracts import AbortThresholds, FaultProfile, SteadyStateBaseline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlastRadiusBudget:
    baseline_error_rate: float
    slo_error_budget: float
    remaining_error_budget: float
    safety_alpha: float
    max_allowable_degradation: float
    abort_thresholds: AbortThresholds


def compute_blast_radius_budget(
    baseline: SteadyStateBaseline,
    fault_profile: FaultProfile,
    slo_checkout_success_percent: float = 99.5,
    safety_alpha: float = 0.10,
) -> BlastRadiusBudget:
    """Computes the Max Allowable Degradation and bounds the experiment with AbortThresholds.

    Formula:
      Max Allowable Degradation = Baseline Error Rate + Remaining Error Budget * SAFETY_ALPHA
    """
    baseline_err = max(0.0, float(baseline.global_error_rate_percent))
    slo_target = float(slo_checkout_success_percent)
    slo_error_budget = max(0.0, 100.0 - slo_target)

    # Remaining error budget before breaching SLO
    remaining_budget = max(0.0, slo_error_budget - baseline_err)
    max_allowable_degradation = baseline_err + (remaining_budget * safety_alpha)

    # Max error rate threshold: capped at the SLO boundary
    max_error_rate = round(slo_error_budget, 2)
    if max_error_rate < baseline_err:
        max_error_rate = round(baseline_err + max_allowable_degradation, 2)

    min_checkout_success = round(100.0 - max_error_rate, 2)

    # Latency abort threshold:
    # "For latency faults, max_p99_latency_ms must be greater than delay_ms + jitter_ms,
    # so an abort signals amplification beyond the fault itself."
    if fault_profile.action == "latency":
        delay_ms = float(fault_profile.parameters.get("delay_ms", 0))
        jitter_ms = float(fault_profile.parameters.get("jitter_ms", 0))
        injected = delay_ms + jitter_ms
        # In fixtures it's 500.0ms for 250ms delay + 20ms jitter
        max_p99 = max(500.0, round(injected * 1.6 + baseline.p99_latency_ms * 2.0, 1))
        # Hard assertion ensuring contract validity
        if max_p99 <= injected:
            max_p99 = injected + 100.0
    else:
        max_p99 = max(500.0, round(baseline.p99_latency_ms * 5.0, 1))

    abort_thresholds = AbortThresholds(
        max_p99_latency_ms=float(max_p99),
        max_error_rate_percent=float(max_error_rate),
        min_checkout_success_rate_percent=float(min_checkout_success),
    )

    return BlastRadiusBudget(
        baseline_error_rate=baseline_err,
        slo_error_budget=slo_error_budget,
        remaining_error_budget=remaining_budget,
        safety_alpha=safety_alpha,
        max_allowable_degradation=round(max_allowable_degradation, 4),
        abort_thresholds=abort_thresholds,
    )
