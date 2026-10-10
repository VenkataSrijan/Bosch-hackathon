"""STUB for Phase 4 (Remediation Advisor). Replace with the real implementation.

Copies fixtures/Recommendation.json into the experiment folder.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common.config import FIXTURES_DIR, REPO_ROOT
from common.contracts import ExperimentPlan, Recommendation
from common.io import EXPERIMENT_PLAN, RECOMMENDATION, read_json, read_model, write_artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase4_advisor", description=__doc__)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    plan = read_model(args.exp / EXPERIMENT_PLAN, ExperimentPlan)
    data = read_json(FIXTURES_DIR / RECOMMENDATION)
    data["experiment_id"] = plan.experiment_id
    write_artifact(args.exp, Recommendation.model_validate(data))
    print("[phase4 stub] wrote Recommendation from fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
