"""STUB for Phase 1 (Observation and Dynamic Profiling). Replace with the real implementation.

Copies fixtures/ExperimentPlan.json into <out>/<experiment_id>/ and prints that folder as the last line.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from common.config import FIXTURES_DIR, get_settings
from common.contracts import ExperimentPlan
from common.io import EXPERIMENT_PLAN, read_model, write_artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase1_observation", description=__doc__)
    parser.add_argument("--out", type=Path, default=None, help="artifacts root (default: ARTIFACTS_DIR)")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args(argv)

    out = args.out or get_settings().artifacts_dir
    plan = read_model(FIXTURES_DIR / EXPERIMENT_PLAN, ExperimentPlan)
    exp_dir = out / plan.experiment_id
    if exp_dir.exists():
        shutil.rmtree(exp_dir)  # the stub always reuses the fixture id; start clean
    write_artifact(exp_dir, plan)
    print(f"[phase1 stub] wrote {EXPERIMENT_PLAN} from fixtures")
    print(exp_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
