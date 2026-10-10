"""STUB for Phase 2 (Autonomous Injection Engine). Replace with the real implementation.

Never touches a cluster. Follows the handshake: receipt RUNNING -> wait for CanaryVerdict ->
NO_GO / GO -> watch for AbortSignal -> final receipt.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from common.config import FIXTURES_DIR, get_settings
from common.contracts import CanaryDecision, CanaryVerdict, ExperimentPlan, InjectionReceipt, InjectionStatus
from common.io import (
    ABORT_SIGNAL, CANARY_VERDICT, EXPERIMENT_PLAN, INJECTION_RECEIPT,
    quarantine_path, read_json, read_model, wait_for_file, write_artifact,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase2_injection", description=__doc__)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    if quarantine_path(args.exp).exists():
        print(f"[phase2 stub] refusing to run: {quarantine_path(args.exp)} exists", file=sys.stderr)
        return 2

    settings = get_settings()
    plan = read_model(args.exp / EXPERIMENT_PLAN, ExperimentPlan)
    data = read_json(FIXTURES_DIR / INJECTION_RECEIPT)
    data["experiment_id"] = plan.experiment_id
    receipt = InjectionReceipt.model_validate(data)
    write_artifact(args.exp, receipt)
    print("[phase2 stub] receipt RUNNING")

    status = InjectionStatus.NO_GO
    if wait_for_file(args.exp / CANARY_VERDICT, settings.canary_verdict_timeout_seconds):
        verdict = read_model(args.exp / CANARY_VERDICT, CanaryVerdict)
        if verdict.decision == CanaryDecision.GO:
            status = InjectionStatus.COMPLETED
            window = 2 if args.mock else plan.fault_profile.max_duration_seconds
            if wait_for_file(args.exp / ABORT_SIGNAL, window):
                status = InjectionStatus.ABORTED

    receipt.status = status
    receipt.timestamps.ended_at = datetime.now(timezone.utc)
    write_artifact(args.exp, receipt)
    print(f"[phase2 stub] receipt {status.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
