"""Phase 3: Diagnostic Judge and Abort Switch (Blue Agent).

Watches the experiment, stops it the moment it breaches the plan's abort thresholds, and explains what
happened from traces and logs. Never applies faults; never writes InjectionReceipt.json.

  python -m phase3_judge --exp artifacts/<experiment_id>                 live (Prometheus, Jaeger, kubectl)
  python -m phase3_judge --exp artifacts/mock/<id> --mock [--mock-scenario abort|healthy]

See phase3_judge/README.md.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from common.config import get_settings
from common.contracts import ExperimentPlan
from common.io import EXPERIMENT_PLAN, read_model

from .judge import Judge
from .telemetry import LiveSource, ReplaySource


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase3_judge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--mock", action="store_true", help="replay recorded telemetry; no cluster")
    parser.add_argument("--mock-scenario", choices=["abort", "healthy"], default="abort",
                        help="recording to replay with --mock: a breach after a GO canary (default), or healthy")
    args = parser.parse_args(argv)

    exp_dir = args.exp.resolve()
    try:
        plan = read_model(exp_dir / EXPERIMENT_PLAN, ExperimentPlan)
    except FileNotFoundError:
        print(f"[phase3] no {EXPERIMENT_PLAN} in {exp_dir}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"[phase3] invalid {EXPERIMENT_PLAN}: {exc}", file=sys.stderr)
        return 1

    settings = get_settings()
    source = (ReplaySource(args.mock_scenario, settings.canary_seconds, plan.target_node.namespace) if args.mock
              else LiveSource(settings.prometheus_url, settings.jaeger_url, plan.target_node.namespace))
    return Judge(exp_dir, plan, source, settings, mock=args.mock, log=lambda m: print(m, flush=True)).run()


if __name__ == "__main__":
    raise SystemExit(main())
