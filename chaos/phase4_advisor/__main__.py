"""Phase 4: Remediation Advisor (Blue Agent).

Reads ExperimentPlan.json and RootCauseAnalysis.json from the experiment folder, localizes the weak call
site in the repo, audits its resilience controls and writes Recommendation.json (plus ResilienceAudit.json
and Recommendation.patch). Never edits src/.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.config import REPO_ROOT
from common.contracts import ExperimentPlan, RootCauseAnalysis
from common.io import EXPERIMENT_PLAN, ROOT_CAUSE_ANALYSIS, read_model, write_artifact, write_json_atomic

from .advisor import build_recommendation, write_audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase4_advisor", description=__doc__)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root")
    parser.add_argument("--mock", action="store_true", help="no LLM; deterministic templates only")
    args = parser.parse_args(argv)

    for name in (EXPERIMENT_PLAN, ROOT_CAUSE_ANALYSIS):
        if not (args.exp / name).exists():
            print(f"[phase4] missing {args.exp / name}", file=sys.stderr)
            return 1
    if not (args.repo / "src").is_dir():
        print(f"[phase4] {args.repo} has no src/ folder; pass the repository root with --repo", file=sys.stderr)
        return 1

    plan = read_model(args.exp / EXPERIMENT_PLAN, ExperimentPlan)
    rca = read_model(args.exp / ROOT_CAUSE_ANALYSIS, RootCauseAnalysis)
    rec, audit = build_recommendation(args.exp, args.repo.resolve(), plan, rca, use_llm=not args.mock)
    options = audit.pop("options", [])
    write_audit(args.exp, audit, rec.suggested_diff)
    write_artifact(args.exp, rec)
    if options:
        write_json_atomic(args.exp / "RecommendationOptions.json",
                          {"experiment_id": plan.experiment_id, "options": options})

    print(f"[phase4] {rec.priority.value} {rec.vulnerability_type}: {rec.summary}")
    if options:
        print(f"[phase4] {len(options)} fix option(s): " + ", ".join(o["title"] for o in options))
    if rec.target_file:
        print(f"[phase4] target {rec.target_file} {rec.target_symbol}")
    patch = audit.get("patch") or {}
    if rec.suggested_diff:
        print(f"[phase4] suggested diff ({patch.get('source')}): {patch.get('check')}")
    elif patch.get("check"):
        print(f"[phase4] no diff: {patch.get('check')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
