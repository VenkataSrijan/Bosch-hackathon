"""The watcher: canary verdict, emergency abort and the final RootCauseAnalysis.

Sequence (live and mock alike; only the source differs):
  wait for InjectionReceipt.json -> sample edge health every second -> at the end of the canary burst
  write CanaryVerdict.json -> on any breach of the plan's abort_thresholds (or MISSING_LIMIT missing
  samples in a row) remove the fault, then write AbortSignal.json and ../QUARANTINE.json -> once the
  receipt is final keep sampling for TAIL_SECONDS of recovery -> RootCauseAnalysis.json.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from common.config import Settings
from common.contracts import (
    AbortSignal, AbortThresholds, CanaryDecision, CanaryVerdict, ExperimentPlan, HealthSample, ImpactSummary,
    InjectionReceipt, InjectionStatus, Quarantine, RcaVerdict, RootCauseAnalysis,
)
from common.io import (
    INJECTION_RECEIPT, append_health_sample, read_model, wait_for_file, write_artifact, write_json_atomic,
    write_quarantine,
)

from .evidence import analyze
from .telemetry import ERROR, METRICS, P99, SUCCESS, Reading

POLL_SECONDS = 1.0
MISSING_LIMIT = 3
TAIL_SECONDS = 30
RECEIPT_TIMEOUT_SECONDS = 120
NO_DATA = -1.0  # AbortSignal.observed_value when the breach is "no data" (the contract needs a number)
JUDGE_LOG = "JudgeLog.json"  # Phase 3's own record: missing samples, abort action, evidence notes

THRESHOLD_FOR = {SUCCESS: "min_checkout_success_rate_percent", ERROR: "max_error_rate_percent",
                 P99: "max_p99_latency_ms"}


@dataclass
class Breach:
    metric: str  # an AbortSignal.breached_metric name
    observed: float
    threshold: float
    detail: str


def check_thresholds(values: dict[str, Optional[float]], th: AbortThresholds) -> Optional[Breach]:
    """The first of the plan's three limits that `values` breaks. Missing values are not judged here."""
    success, error, p99 = values.get(SUCCESS), values.get(ERROR), values.get(P99)
    if success is not None and success < th.min_checkout_success_rate_percent:
        return Breach("min_checkout_success_rate_percent", success, th.min_checkout_success_rate_percent,
                      f"checkout success {success:.2f}% < {th.min_checkout_success_rate_percent:.2f}%")
    if error is not None and error > th.max_error_rate_percent:
        return Breach("max_error_rate_percent", error, th.max_error_rate_percent,
                      f"error rate {error:.2f}% > {th.max_error_rate_percent:.2f}%")
    if p99 is not None and p99 > th.max_p99_latency_ms:
        return Breach("max_p99_latency_ms", p99, th.max_p99_latency_ms,
                      f"p99 {p99:.0f} ms > {th.max_p99_latency_ms:.0f} ms")
    return None


def missing_data_breach(reading: Reading, th: AbortThresholds) -> Breach:
    metric = THRESHOLD_FOR[reading.missing[0]]
    return Breach(metric, NO_DATA, getattr(th, metric),
                  f"no data for {MISSING_LIMIT} consecutive samples ({'; '.join(reading.problems) or 'missing'})")


def degraded(sample: HealthSample, plan: ExperimentPlan) -> bool:
    """True when a sample used up at least half of the margin between the baseline and an abort limit."""
    b, th = plan.steady_state_baseline, plan.abort_thresholds

    def halfway_up(base: float, limit: float) -> float:
        return base + (limit - base) / 2 if limit > base else limit

    return (sample.p99_latency_ms >= halfway_up(b.p99_latency_ms, th.max_p99_latency_ms)
            or sample.error_rate_percent >= halfway_up(b.global_error_rate_percent, th.max_error_rate_percent)
            or sample.checkout_success_rate_percent
            <= b.checkout_success_rate_percent - max(0.0, b.checkout_success_rate_percent
                                                     - th.min_checkout_success_rate_percent) / 2)


class Judge:
    def __init__(self, exp_dir: Path, plan: ExperimentPlan, source: Any, settings: Settings, *,
                 mock: bool, log: Callable[[str], None] = print):
        self.exp_dir = Path(exp_dir)
        self.plan = plan
        self.source = source
        self.settings = settings
        self.mock = mock
        self.log = log
        self.eid = plan.experiment_id
        self.samples: list[HealthSample] = []
        self.record: dict[str, Any] = {"experiment_id": self.eid, "missing_samples": [], "abort": None,
                                       "canary": None, "evidence": None}

    # ---------------------------------------------------------------- run

    def run(self) -> int:
        receipt_path = self.exp_dir / INJECTION_RECEIPT
        if not wait_for_file(receipt_path, RECEIPT_TIMEOUT_SECONDS, poll_s=0.05 if self.mock else 0.5):
            self.log(f"[phase3] no {INJECTION_RECEIPT} after {RECEIPT_TIMEOUT_SECONDS} s")
            return 1
        receipt = read_model(receipt_path, InjectionReceipt)
        injected_at = receipt.timestamps.initiated_at
        self.source.begin(injected_at)
        self.log(f"[phase3] receipt {receipt.status.value} for {self.eid}; watching edge health "
                 f"({'mock replay' if self.mock else 'live'})")
        if receipt.status == InjectionStatus.FAILED:
            self.log("[phase3] receipt FAILED: no fault was applied")

        canary_end = injected_at + timedelta(seconds=self.settings.canary_seconds)
        canary_done = receipt.is_final
        missing_run = 0
        abort: Optional[AbortSignal] = None
        final_at: Optional[datetime] = None
        give_up = time.monotonic() + (60 if self.mock else max(
            0.0, (receipt.timestamps.hard_timeout_at - self.source.now()).total_seconds())
            + self.settings.canary_verdict_timeout_seconds + 60)

        while True:
            receipt = self._reread(receipt_path, receipt)
            now = self.source.now()
            if final_at is None and receipt.is_final:
                final_at = now
                self.log(f"[phase3] receipt final: {receipt.status.value}; recording {TAIL_SECONDS} s of recovery")
            if final_at is not None and now >= final_at + timedelta(seconds=TAIL_SECONDS):
                break
            if time.monotonic() > give_up:
                self.log("[phase3] receipt never became final; stopping the watch")
                final_at = final_at or now
                break

            reading = self.source.read()
            if reading.missing:
                missing_run += 1
                self._record_missing(reading)
            else:
                missing_run = 0
                sample = HealthSample(t=reading.t, **{m: reading.values[m] for m in METRICS})
                self.samples.append(sample)
                append_health_sample(self.exp_dir, sample)

            if abort is None and final_at is None:
                breach = check_thresholds(reading.values, self.plan.abort_thresholds)
                if breach is None and missing_run >= MISSING_LIMIT:
                    breach = missing_data_breach(reading, self.plan.abort_thresholds)
                if breach is not None:
                    abort = self._abort(breach)

            if not canary_done and (abort is not None or now >= canary_end):
                self._canary_verdict(injected_at, canary_end, abort)
                canary_done = True

            self.source.sleep(POLL_SECONDS)

        ended_at = min(abort.aborted_at, final_at) if abort else final_at
        self._write_rca(receipt, injected_at, ended_at, abort)
        return 0

    # ---------------------------------------------------------------- steps

    def _reread(self, path: Path, previous: InjectionReceipt) -> InjectionReceipt:
        try:
            return read_model(path, InjectionReceipt)
        except Exception:  # mid-write or briefly unreadable: keep the last good copy
            return previous

    def _record_missing(self, reading: Reading) -> None:
        self.record["missing_samples"].append(
            {"t": reading.t.isoformat(), "missing": reading.missing, "problems": reading.problems})
        self.log(f"[phase3] sample missing at {reading.t.isoformat()}: {'; '.join(reading.problems)}")
        self._save_record()

    def _canary_verdict(self, injected_at: datetime, canary_end: datetime, abort: Optional[AbortSignal]) -> None:
        window = [s for s in self.samples if injected_at <= s.t <= canary_end]
        if abort is not None:
            decision, reason = CanaryDecision.NO_GO, f"aborted during the canary burst: {self.record['abort']['detail']}"
        elif not window:
            decision, reason = CanaryDecision.NO_GO, "no edge health samples during the canary burst (fail safe)"
        else:
            decision = CanaryDecision.GO
            reason = (f"{len(window)} samples within all abort thresholds during the canary burst: lowest checkout "
                      f"success {min(s.checkout_success_rate_percent for s in window):.2f}%, highest error rate "
                      f"{max(s.error_rate_percent for s in window):.2f}%, highest p99 "
                      f"{max(s.p99_latency_ms for s in window):.0f} ms")
        verdict = CanaryVerdict(experiment_id=self.eid, decision=decision, checked_at=self.source.now(), reason=reason)
        write_artifact(self.exp_dir, verdict)
        self.record["canary"] = {"decision": decision.value, "samples": len(window), "reason": reason}
        self._save_record()
        self.log(f"[phase3] canary verdict {decision.value}: {reason}")

    def _abort(self, breach: Breach) -> AbortSignal:
        self.log(f"[phase3] ABORT: {breach.detail}")
        removed, how = self.source.remove_fault(self.eid)  # first stop the fault, then tell everyone
        self.log(f"[phase3] fault removal {'done' if removed else 'FAILED'}: {how}")
        now = self.source.now()
        signal = AbortSignal(experiment_id=self.eid, aborted_at=now, breached_metric=breach.metric,
                             observed_value=round(breach.observed, 4), threshold=breach.threshold)
        write_artifact(self.exp_dir, signal)
        write_quarantine(self.exp_dir, Quarantine(
            experiment_id=self.eid, quarantined_at=now,
            reason=f"{breach.detail}; fault removal {'done' if removed else 'FAILED'}: {how}"))
        self.record["abort"] = {"detail": breach.detail, "fault_removed": removed, "removal": how,
                                "aborted_at": now.isoformat()}
        self._save_record()
        return signal

    def _write_rca(self, receipt: InjectionReceipt, injected_at: datetime, ended_at: datetime,
                   abort: Optional[AbortSignal]) -> None:
        during = [s for s in self.samples if s.t >= injected_at]
        if abort is not None:
            verdict = RcaVerdict.ABORT_TRIGGERED
        elif any(degraded(s, self.plan) for s in during):
            verdict = RcaVerdict.DEGRADED_NO_ABORT
        else:
            verdict = RcaVerdict.STEADY_STATE_MAINTAINED

        if during:
            lowest = min(s.checkout_success_rate_percent for s in during)
            peak = max(s.p99_latency_ms for s in during)
        else:
            base = self.plan.steady_state_baseline
            lowest, peak = base.checkout_success_rate_percent, base.p99_latency_ms
            self.log("[phase3] no valid samples; impact summary falls back to the plan's measured baseline")
            self.record["impact_summary_source"] = "ExperimentPlan.steady_state_baseline (no valid samples)"

        cascade, root_cause = [], None
        if verdict != RcaVerdict.STEADY_STATE_MAINTAINED:
            findings = analyze(self.source, self.plan, receipt, injected_at, ended_at)
            cascade, root_cause = findings.cascade_path, findings.root_cause
            self.record["evidence"] = {"notes": findings.notes, "edges": findings.edges}
            self._save_record()
            for note in findings.notes:
                self.log(f"[phase3] evidence: {note}")

        rca = RootCauseAnalysis(
            experiment_id=self.eid, verdict=verdict,
            impact_summary=ImpactSummary(steady_state_maintained=verdict == RcaVerdict.STEADY_STATE_MAINTAINED,
                                         lowest_checkout_success_rate=round(lowest, 4),
                                         peak_p99_latency_ms=round(peak, 2)),
            cascade_path=cascade, root_cause_isolation=root_cause)
        write_artifact(self.exp_dir, rca)
        path = " -> ".join(h.service for h in cascade) or "none"
        self.log(f"[phase3] RootCauseAnalysis {verdict.value}; cascade {path}"
                 + (f"; faulty service {root_cause.faulty_service}" if root_cause else ""))

    def _save_record(self) -> None:
        write_json_atomic(self.exp_dir / JUDGE_LOG, self.record)
