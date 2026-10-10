"""STUB for Phase 3 (Diagnostic Judge and Abort Switch). Replace with the real implementation.

Waits for the receipt, then writes CanaryVerdict, AbortSignal, HealthSeries and RootCauseAnalysis
from fixtures. It does not write QUARANTINE.json, so repeated mock runs are not blocked.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from common.config import FIXTURES_DIR
from common.contracts import (
    AbortSignal, CanaryVerdict, ExperimentPlan, HealthSeries, InjectionReceipt, RootCauseAnalysis,
)
from common.io import (
    ABORT_SIGNAL, CANARY_VERDICT, EXPERIMENT_PLAN, HEALTH_SERIES, INJECTION_RECEIPT, ROOT_CAUSE_ANALYSIS,
    read_json, read_model, wait_for_file, write_artifact,
)

RECEIPT_TIMEOUT_SECONDS = 120


def _fixture(name: str, model_cls, experiment_id: str):
    data = read_json(FIXTURES_DIR / name)
    data["experiment_id"] = experiment_id
    return model_cls.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase3_judge", description=__doc__)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    plan = read_model(args.exp / EXPERIMENT_PLAN, ExperimentPlan)
    if not wait_for_file(args.exp / INJECTION_RECEIPT, RECEIPT_TIMEOUT_SECONDS):
        print("[phase3 stub] no InjectionReceipt.json after 120 s", file=sys.stderr)
        return 1

    eid = plan.experiment_id
    write_artifact(args.exp, HealthSeries.model_validate(read_json(FIXTURES_DIR / HEALTH_SERIES)))
    write_artifact(args.exp, _fixture(CANARY_VERDICT, CanaryVerdict, eid))
    write_artifact(args.exp, _fixture(ABORT_SIGNAL, AbortSignal, eid))
    print("[phase3 stub] wrote HealthSeries, CanaryVerdict, AbortSignal")

    deadline = time.monotonic() + plan.fault_profile.max_duration_seconds + 30
    while time.monotonic() < deadline:
        if read_model(args.exp / INJECTION_RECEIPT, InjectionReceipt).is_final:
            break
        time.sleep(0.5)

    write_artifact(args.exp, _fixture(ROOT_CAUSE_ANALYSIS, RootCauseAnalysis, eid))
    print("[phase3 stub] wrote RootCauseAnalysis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
