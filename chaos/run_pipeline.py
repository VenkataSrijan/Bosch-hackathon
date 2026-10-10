"""Run the four phases in order, following the runtime sequence.

  python run_pipeline.py --mock      no cluster, no API key (artifacts go to <ARTIFACTS_DIR>/mock)
  python run_pipeline.py             live, against the cluster in the current kube context

1. Phase 1 writes ExperimentPlan.json and prints the experiment folder.
2. Phase 3 starts in the background and waits for InjectionReceipt.json.
3. Phase 2 runs the canary handshake and the injection.
4. Phase 3 finishes with RootCauseAnalysis.json.
5. Phase 4 writes Recommendation.json.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from common.config import CHAOS_ROOT, REPO_ROOT, get_settings
from common.contracts import ExperimentPlan
from common.io import (
    EXPERIMENT_PLAN, INJECTION_RECEIPT, QUARANTINE, RECOMMENDATION, ROOT_CAUSE_ANALYSIS,
    read_artifact, read_model,
)

PHASE3_RECEIPT_WAIT_SECONDS = 120


def _cmd(module: str, *args: str, mock: bool) -> list[str]:
    return [sys.executable, "-m", module, *args, *(["--mock"] if mock else [])]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd[1:])}", flush=True)
    return subprocess.run(cmd, cwd=CHAOS_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock", action="store_true", help="run every phase with --mock")
    parser.add_argument("--artifacts", type=Path, default=None,
                        help="artifacts root (default: ARTIFACTS_DIR, or ARTIFACTS_DIR/mock with --mock)")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="repository root passed to Phase 4")
    args = parser.parse_args(argv)

    settings = get_settings()
    root = (args.artifacts or (settings.artifacts_dir / "mock" if args.mock else settings.artifacts_dir)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    quarantine = root / QUARANTINE
    if quarantine.exists():
        if args.mock:
            print(f"[pipeline] mock run: clearing mock quarantine {quarantine}")
            quarantine.unlink()
        else:
            print(f"[pipeline] {quarantine} exists: an earlier experiment aborted. "
                  "Acknowledge it in the console before injecting again.", file=sys.stderr)
            return 3

    # 1. Phase 1
    cmd = _cmd("phase1_observation", "--out", str(root), mock=args.mock)
    print(f"\n$ {' '.join(cmd[1:])}", flush=True)
    p1 = subprocess.run(cmd, cwd=CHAOS_ROOT, stdout=subprocess.PIPE, text=True)
    sys.stdout.write(p1.stdout)
    lines = [ln for ln in p1.stdout.splitlines() if ln.strip()]
    if p1.returncode != 0 or not lines:
        print("[pipeline] Phase 1 failed", file=sys.stderr)
        return p1.returncode or 1
    exp_dir = Path(lines[-1].strip())
    if not exp_dir.is_absolute():
        exp_dir = (CHAOS_ROOT / exp_dir).resolve()
    plan = read_model(exp_dir / EXPERIMENT_PLAN, ExperimentPlan)

    # 2. Phase 3 in the background
    p3_cmd = _cmd("phase3_judge", "--exp", str(exp_dir), mock=args.mock)
    print(f"\n$ {' '.join(p3_cmd[1:])} &", flush=True)
    p3 = subprocess.Popen(p3_cmd, cwd=CHAOS_ROOT)

    # 3. Phase 2
    p2 = _run(_cmd("phase2_injection", "--exp", str(exp_dir), mock=args.mock))

    # 4. Wait for Phase 3
    p3_timeout = (PHASE3_RECEIPT_WAIT_SECONDS + settings.canary_seconds
                  + settings.canary_verdict_timeout_seconds + plan.fault_profile.max_duration_seconds + 60)
    try:
        p3_code = p3.wait(timeout=p3_timeout)
    except subprocess.TimeoutExpired:
        p3.kill()
        print(f"[pipeline] Phase 3 did not finish within {p3_timeout} s; killed", file=sys.stderr)
        p3_code = 1

    # 5. Phase 4 (only if there is an RCA to advise on)
    p4_code = 0
    if (exp_dir / ROOT_CAUSE_ANALYSIS).exists():
        p4_code = _run(_cmd("phase4_advisor", "--exp", str(exp_dir), "--repo", str(args.repo.resolve()),
                            mock=args.mock)).returncode
    else:
        print("[pipeline] no RootCauseAnalysis.json, skipping Phase 4", file=sys.stderr)
        p4_code = 1

    receipt = read_artifact(exp_dir, INJECTION_RECEIPT)
    rca = read_artifact(exp_dir, ROOT_CAUSE_ANALYSIS)
    rec = read_artifact(exp_dir, RECOMMENDATION)
    print("\n=== pipeline summary ===")
    print(f"experiment      {plan.experiment_id}  ({exp_dir})")
    print(f"phase exit      p1=0 p2={p2.returncode} p3={p3_code} p4={p4_code}")
    print(f"injection       {receipt.status.value if receipt else 'no receipt'}")
    print(f"verdict         {rca.verdict.value if rca else 'no RCA'}")
    print(f"recommendation  {rec.priority.value + ' ' + (rec.target_file or '-') if rec else 'none'}")
    print(f"quarantine      {'SET' if quarantine.exists() else 'clear'}")
    return 0 if p2.returncode == 0 and p3_code == 0 and p4_code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
