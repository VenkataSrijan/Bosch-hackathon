"""Pydantic models for every artifact exchanged between phases.

These models ARE the contract. Field names and nesting match chaos/fixtures/ exactly.
Change them only through a commit on main after the team agrees, then regenerate
chaos/contracts/*.schema.json with `python -m common.schemas`.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel, model_validator

JSON_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class _Document(_Model):
    """Top-level artifact that carries a "$schema" key."""

    schema_uri: str = Field(default=JSON_SCHEMA_URI, alias="$schema")


# ---------------------------------------------------------------- enums

class InjectionStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    NO_GO = "NO_GO"
    FAILED = "FAILED"


FINAL_INJECTION_STATUSES = frozenset(
    {InjectionStatus.COMPLETED, InjectionStatus.ABORTED, InjectionStatus.NO_GO, InjectionStatus.FAILED}
)


class CanaryDecision(str, Enum):
    GO = "GO"
    NO_GO = "NO_GO"


class RcaVerdict(str, Enum):
    ABORT_TRIGGERED = "ABORT_TRIGGERED"
    DEGRADED_NO_ABORT = "DEGRADED_NO_ABORT"
    STEADY_STATE_MAINTAINED = "STEADY_STATE_MAINTAINED"


class Priority(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


ThresholdName = Literal[
    "max_p99_latency_ms", "max_error_rate_percent", "min_checkout_success_rate_percent"
]


# ---------------------------------------------------------------- ExperimentPlan.json (Phase 1)

class TargetNode(_Model):
    service_name: str
    namespace: str
    deployment: str
    replicas_total: int = Field(ge=2)
    targetable_replicas: int = Field(ge=1)

    @model_validator(mode="after")
    def _leave_one_replica_untouched(self) -> "TargetNode":
        if self.targetable_replicas > self.replicas_total - 1:
            raise ValueError("targetable_replicas must be at most replicas_total - 1")
        return self


class FaultProfile(_Model):
    category: str
    action: str
    parameters: dict[str, Any]
    ramp_strategy: str
    max_duration_seconds: int = Field(gt=0)


class SteadyStateBaseline(_Model):
    p99_latency_ms: float = Field(ge=0)
    global_error_rate_percent: float = Field(ge=0, le=100)
    checkout_success_rate_percent: float = Field(ge=0, le=100)


class AbortThresholds(_Model):
    max_p99_latency_ms: float = Field(gt=0)
    max_error_rate_percent: float = Field(ge=0, le=100)
    min_checkout_success_rate_percent: float = Field(ge=0, le=100)


class ExperimentPlan(_Document):
    experiment_id: str = Field(pattern=r"^exp-[a-z0-9]+-[a-z0-9-]+-\d{3}$")
    created_at: AwareDatetime
    target_node: TargetNode
    fault_profile: FaultProfile
    steady_state_baseline: SteadyStateBaseline
    abort_thresholds: AbortThresholds

    @model_validator(mode="after")
    def _latency_threshold_above_fault(self) -> "ExperimentPlan":
        fp = self.fault_profile
        if fp.action == "latency":
            injected = float(fp.parameters.get("delay_ms", 0)) + float(fp.parameters.get("jitter_ms", 0))
            if self.abort_thresholds.max_p99_latency_ms <= injected:
                raise ValueError("max_p99_latency_ms must exceed delay_ms + jitter_ms for latency faults")
        return self


# ---------------------------------------------------------------- InjectionReceipt.json (Phase 2)

class ExecutionDetails(_Model):
    target_pod: str
    cgroup_id: str
    fault_applied: str
    pod_scope: str


class InjectionTimestamps(_Model):
    initiated_at: AwareDatetime
    hard_timeout_at: AwareDatetime
    ended_at: Optional[AwareDatetime] = None


class InjectionReceipt(_Document):
    experiment_id: str
    injection_id: str
    status: InjectionStatus
    execution_details: ExecutionDetails
    timestamps: InjectionTimestamps

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_INJECTION_STATUSES


# ---------------------------------------------------------------- Phase 3 artifacts

class CanaryVerdict(_Model):
    experiment_id: str
    decision: CanaryDecision
    checked_at: AwareDatetime
    reason: str


class AbortSignal(_Model):
    experiment_id: str
    aborted_at: AwareDatetime
    breached_metric: ThresholdName
    observed_value: float
    threshold: float


class HealthSample(_Model):
    t: AwareDatetime
    checkout_success_rate_percent: float
    p99_latency_ms: float
    error_rate_percent: float


class HealthSeries(RootModel[list[HealthSample]]):
    pass


class ImpactSummary(_Model):
    steady_state_maintained: bool
    lowest_checkout_success_rate: float
    peak_p99_latency_ms: float


class CascadeHop(_Model):
    hop: int = Field(ge=1)
    service: str
    manifestation: str


class RootCauseIsolation(_Model):
    faulty_service: str
    mechanism: str
    stack_trace_snippet: Optional[str] = None


class RootCauseAnalysis(_Document):
    experiment_id: str
    verdict: RcaVerdict
    impact_summary: ImpactSummary
    cascade_path: list[CascadeHop] = Field(default_factory=list)
    root_cause_isolation: Optional[RootCauseIsolation] = None


# ---------------------------------------------------------------- Recommendation.json (Phase 4)

class Recommendation(_Document):
    experiment_id: str
    recommendation_id: str
    priority: Priority
    target_repository: str
    target_file: Optional[str] = None
    target_symbol: Optional[str] = None
    vulnerability_type: str
    summary: str
    architectural_guidance: str
    suggested_diff: Optional[str] = None
    verification_command: str

    @model_validator(mode="after")
    def _info_has_no_diff(self) -> "Recommendation":
        if self.priority == Priority.INFO and self.suggested_diff is not None:
            raise ValueError("suggested_diff must be null when priority is INFO")
        return self


# ---------------------------------------------------------------- ../QUARANTINE.json (Phase 3, cleared by the console)

class Quarantine(_Model):
    experiment_id: str
    quarantined_at: AwareDatetime
    reason: str


# ---------------------------------------------------------------- registry

ARTIFACT_MODELS: dict[str, type[BaseModel]] = {
    "ExperimentPlan.json": ExperimentPlan,
    "InjectionReceipt.json": InjectionReceipt,
    "CanaryVerdict.json": CanaryVerdict,
    "AbortSignal.json": AbortSignal,
    "HealthSeries.json": HealthSeries,
    "RootCauseAnalysis.json": RootCauseAnalysis,
    "Recommendation.json": Recommendation,
}

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    **{name.removesuffix(".json"): model for name, model in ARTIFACT_MODELS.items()},
    "Quarantine": Quarantine,
}
