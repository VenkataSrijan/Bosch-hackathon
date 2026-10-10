"""The Autonomous Injection Engine (Phase 2, Red Agent).

Turns an ExperimentPlan into a real Chaos Mesh fault, safely, following the
contract handshake with the Blue Agent:

    pre-flight -> apply (canary) -> receipt RUNNING -> remove ->
    wait for CanaryVerdict -> NO_GO/timeout: stop  |  GO: re-apply for the
    rest of the window -> watch for AbortSignal -> final receipt.

Safety: every applied resource carries a Chaos Mesh ``duration`` no longer than
its window (a dead-man's switch, so the fault self-clears if this process dies),
and the engine deletes its resources on normal exit, on an abort, on SIGINT /
SIGTERM and on any unhandled exception.

Phase 2 never reads telemetry, logs or source code and never judges a result.
"""
from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import randint
from typing import Callable, Optional

from pydantic import ValidationError

from common.config import Settings, get_settings
from common.contracts import (
    CanaryDecision, CanaryVerdict, ExecutionDetails, ExperimentPlan,
    InjectionReceipt, InjectionStatus, InjectionTimestamps,
)
from common.io import (
    ABORT_SIGNAL, CANARY_VERDICT, EXPERIMENT_PLAN, quarantine_path,
    read_model, wait_for_file, write_artifact,
)

from .kube import ClusterInjector, Injector, InjectorError, MockInjector
from .manifest import (
    UnsupportedFault, build_manifest, describe_fault, manifest_filename, write_manifest,
)

# In mock mode no fault is really active, so waits are capped hard to keep runs
# and tests fast while still honouring "never sleep more than 1 second per wait".
MOCK_WINDOW_CAP_SECONDS = 0.25
LIVE_ABORT_POLL_SECONDS = 0.5
MOCK_ABORT_POLL_SECONDS = 0.05


class PreflightError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _abbrev(category: str) -> str:
    return {"network": "net", "pod": "pod", "stress": "str"}.get((category or "").lower(), "flt")


class InjectionEngine:
    def __init__(
        self,
        exp_dir: Path,
        plan: ExperimentPlan,
        settings: Settings,
        injector: Injector,
        *,
        mock: bool,
        log: Callable[[str], None] = print,
    ) -> None:
        self.exp_dir = Path(exp_dir)
        self.plan = plan
        self.settings = settings
        self.injector = injector
        self.mock = mock
        self.log = log

        self.experiment_id = plan.experiment_id
        self.namespace = plan.target_node.namespace
        self.max_duration = plan.fault_profile.max_duration_seconds
        self.injection_id = f"inj-{_abbrev(plan.fault_profile.category)}-{randint(0, 999999):06d}"

        # Never touch the last replica: clamp target_pods to targetable_replicas.
        requested = int(plan.fault_profile.parameters.get("target_pods", 1) or 1)
        self.target_pods = max(1, min(requested, plan.target_node.targetable_replicas))

        self.manifest_path = self.exp_dir / manifest_filename(self.experiment_id, "NetworkChaos")
        self.kind = "NetworkChaos"
        self.fault_applied = "none"
        self.target_pod = "n/a"
        self.cgroup_id = "n/a"
        self.pod_scope = f"{self.target_pods} of {plan.target_node.replicas_total} replicas"

        self.initiated_at: Optional[datetime] = None
        self.hard_timeout_at: Optional[datetime] = None
        self._applied = False
        self._cleaned = False
        self._orig_handlers: dict = {}

    # ---------------------------------------------------------------- public

    def run(self) -> int:
        fp = self.plan.fault_profile
        self.log(f"[phase2] experiment {self.experiment_id}: fault {fp.category}/{fp.action} "
                 f"on {self.plan.target_node.service_name} ({'mock' if self.mock else 'live'})")

        try:
            self._preflight()
        except (PreflightError, InjectorError) as exc:
            self.log(f"[phase2] pre-flight failed: {exc}")
            self.fault_applied = f"not applied (pre-flight failed: {exc})"
            self.initiated_at = self.hard_timeout_at = _utcnow()
            self._write_receipt(InjectionStatus.FAILED, ended=True)
            return 1

        kind, manifest = build_manifest(self.plan, duration_seconds=self.max_duration, target_pods=self.target_pods)
        self.kind = kind
        self.manifest_path = self.exp_dir / manifest_filename(self.experiment_id, kind)
        write_manifest(self.manifest_path, manifest)
        self.fault_applied = describe_fault(kind, manifest)
        self.log(f"[phase2] wrote manifest {self.manifest_path.name} ({kind})")

        self._install_signal_handlers()
        try:
            return self._inject()
        except BaseException as exc:  # includes SystemExit / KeyboardInterrupt
            self.log(f"[phase2] aborting on {type(exc).__name__}: {exc}")
            if self.initiated_at is not None:
                try:
                    self._write_receipt(InjectionStatus.FAILED, ended=True)
                except Exception:
                    pass
            raise
        finally:
            self._cleanup()
            self._restore_signal_handlers()

    # ---------------------------------------------------------------- sequence

    def _inject(self) -> int:
        self.initiated_at = _utcnow()
        self.hard_timeout_at = self.initiated_at + timedelta(seconds=self.max_duration)

        # Canary ramp: apply a short burst, publish the receipt, watch for an abort.
        self._apply(self.settings.canary_seconds)
        self._write_receipt(InjectionStatus.RUNNING)
        self.log("[phase2] receipt RUNNING; canary burst")
        if self._watch_for_abort(self.settings.canary_seconds):
            return self._finish(InjectionStatus.ABORTED)
        self._remove()  # end the canary burst; the system recovers before the verdict

        # Wait for the Blue Agent's go/no-go.
        poll = MOCK_ABORT_POLL_SECONDS if self.mock else LIVE_ABORT_POLL_SECONDS
        if not wait_for_file(self.exp_dir / CANARY_VERDICT, self.settings.canary_verdict_timeout_seconds, poll_s=poll):
            self.log("[phase2] no CanaryVerdict before timeout -> NO_GO")
            return self._finish(InjectionStatus.NO_GO)
        verdict = read_model(self.exp_dir / CANARY_VERDICT, CanaryVerdict)
        if verdict.decision != CanaryDecision.GO:
            self.log(f"[phase2] canary verdict NO_GO: {verdict.reason}")
            return self._finish(InjectionStatus.NO_GO)
        self.log("[phase2] canary verdict GO; full injection")

        # Re-apply for the remainder of the window (durative faults only).
        remaining = (self.hard_timeout_at - _utcnow()).total_seconds()
        if "duration" not in build_manifest(self.plan, duration_seconds=1, target_pods=self.target_pods)[1]["spec"]:
            self.log("[phase2] one-shot fault; nothing to re-apply")
            return self._finish(InjectionStatus.COMPLETED)
        if remaining < 1:
            self.log("[phase2] window already elapsed; completing")
            return self._finish(InjectionStatus.COMPLETED)
        self._apply(remaining)
        aborted = self._watch_for_abort(remaining)
        self._remove()
        return self._finish(InjectionStatus.ABORTED if aborted else InjectionStatus.COMPLETED)

    # ---------------------------------------------------------------- steps

    def _preflight(self) -> None:
        if quarantine_path(self.exp_dir).exists():
            raise PreflightError("QUARANTINE.json exists")
        try:
            build_manifest(self.plan, duration_seconds=self.max_duration, target_pods=self.target_pods)
        except UnsupportedFault as exc:
            raise PreflightError(str(exc)) from exc

        tn = self.plan.target_node
        ready = self.injector.ready_replicas(self.namespace, tn.deployment)
        if ready < tn.replicas_total:
            raise PreflightError(
                f"deployment {tn.deployment} has {ready} ready replicas, expected {tn.replicas_total}"
            )
        pods = self.injector.pods_for(self.namespace, tn.service_name)
        if not pods:
            raise PreflightError(f"no running pods for app={tn.service_name}")
        self.target_pod = pods[0]
        uid = self.injector.pod_uid(self.namespace, self.target_pod)
        self.cgroup_id = f"pod:{uid}" if uid else "unknown"

    def _apply(self, window_seconds: float) -> None:
        kind, manifest = build_manifest(self.plan, duration_seconds=window_seconds, target_pods=self.target_pods)
        write_manifest(self.manifest_path, manifest)
        self.injector.apply(self.manifest_path)
        self._applied = True
        self.log(f"[phase2] applied {kind} (dead-man duration ~{int(round(window_seconds))}s)")

    def _remove(self) -> None:
        if not self._applied:
            return
        self.injector.delete_by_label(self.namespace, self.experiment_id)
        self._applied = False
        self.log("[phase2] removed fault")

    def _watch_for_abort(self, seconds: float) -> bool:
        """Sleep up to ``seconds``, polling for AbortSignal. True if an abort appears."""
        abort_path = self.exp_dir / ABORT_SIGNAL
        window = min(seconds, MOCK_WINDOW_CAP_SECONDS) if self.mock else seconds
        poll = MOCK_ABORT_POLL_SECONDS if self.mock else LIVE_ABORT_POLL_SECONDS
        deadline = time.monotonic() + window
        while True:
            if abort_path.exists():
                self.log("[phase2] AbortSignal seen; stopping immediately")
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll, remaining))

    def _finish(self, status: InjectionStatus) -> int:
        self._write_receipt(status, ended=True)
        self.log(f"[phase2] final status {status.value}")
        return 0

    # ---------------------------------------------------------------- receipt

    def _write_receipt(self, status: InjectionStatus, *, ended: bool = False) -> None:
        now = _utcnow()
        initiated = self.initiated_at or now
        receipt = InjectionReceipt(
            experiment_id=self.experiment_id,
            injection_id=self.injection_id,
            status=status,
            execution_details=ExecutionDetails(
                target_pod=self.target_pod,
                cgroup_id=self.cgroup_id,
                fault_applied=self.fault_applied,
                pod_scope=self.pod_scope,
            ),
            timestamps=InjectionTimestamps(
                initiated_at=initiated,
                hard_timeout_at=self.hard_timeout_at or initiated,
                ended_at=now if ended else None,
            ),
        )
        write_artifact(self.exp_dir, receipt)

    # ---------------------------------------------------------------- cleanup

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.injector.delete_by_label(self.namespace, self.experiment_id)
            self._applied = False
        except Exception as exc:  # cleanup must never mask the original outcome
            self.log(f"[phase2] cleanup warning: {exc}")

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            self.log(f"[phase2] signal {signum}: cleaning up and exiting")
            self._cleanup()
            raise SystemExit(128 + signum)

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self._orig_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not the main thread / unsupported platform
                pass

    def _restore_signal_handlers(self) -> None:
        for sig, original in self._orig_handlers.items():
            try:
                signal.signal(sig, original)
            except (ValueError, OSError):
                pass
        self._orig_handlers.clear()


def run(
    exp_dir: Path | str,
    *,
    mock: bool,
    settings: Optional[Settings] = None,
    injector: Optional[Injector] = None,
    log: Callable[[str], None] = print,
) -> int:
    """Entry point used by ``__main__`` and the tests."""
    exp_dir = Path(exp_dir)
    settings = settings or get_settings()

    qpath = quarantine_path(exp_dir)
    if qpath.exists():
        print(f"[phase2] refusing to run: {qpath} exists (an earlier experiment aborted)", file=sys.stderr)
        return 2

    try:
        plan = read_model(exp_dir / EXPERIMENT_PLAN, ExperimentPlan)
    except FileNotFoundError:
        print(f"[phase2] no {EXPERIMENT_PLAN} in {exp_dir}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"[phase2] invalid ExperimentPlan: {exc}", file=sys.stderr)
        return 1

    if injector is None:
        injector = MockInjector(plan.target_node.replicas_total) if mock else ClusterInjector()

    return InjectionEngine(exp_dir, plan, settings, injector, mock=mock, log=log).run()
