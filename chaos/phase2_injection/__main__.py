"""Phase 2: Autonomous Injection Engine (Red Agent).

Turns ExperimentPlan.json into a real Chaos Mesh fault, safely, and reports what
it did in InjectionReceipt.json. No telemetry reads, no source-code reads, no
judgement of results.

    python -m phase2_injection --exp <experiment folder> [--mock]

Exit code 0 on success, including a clean abort or a NO_GO. Non-zero only on a
real failure (bad plan, quarantine, pre-flight failure).
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .engine import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="phase2_injection", description=__doc__)
    parser.add_argument("--exp", type=Path, required=True, help="experiment folder")
    parser.add_argument("--mock", action="store_true",
                        help="no cluster; follow the same handshake with waits capped at ~1 second")
    args = parser.parse_args(argv)
    return run(args.exp, mock=args.mock)


if __name__ == "__main__":
    raise SystemExit(main())
