"""Chaos Mesh manifest synthesis from an ExperimentPlan.

Phase 2 turns the ``fault_profile`` in an ExperimentPlan into a Chaos Mesh custom
resource. The default and demo path is a NetworkChaos *latency* fault; packet
loss (NetworkChaos), pod-kill (PodChaos) and CPU / memory stress (StressChaos)
are also mapped (stretch goal).

Nothing here touches a cluster, telemetry or source code: a plan goes in, a
manifest dict comes out. Serialisation prefers PyYAML for readable YAML and
falls back to JSON, which is valid YAML, so the module imports with no extra
dependency installed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from common.contracts import ExperimentPlan

LABEL_KEY = "chaos.ai/experiment-id"
CHAOS_API_VERSION = "chaos-mesh.org/v1alpha1"

# (category, action) pairs we understand, for error messages and docs.
_LATENCY_ACTIONS = {"latency", "delay"}
_LOSS_ACTIONS = {"loss", "packet-loss", "packet_loss"}
_KILL_ACTIONS = {"kill", "pod-kill", "pod_kill", "podkill"}
_CPU_ACTIONS = {"cpu", "cpu-stress", "cpu_stress"}
_MEM_ACTIONS = {"memory", "mem", "memory-stress", "memory_stress"}


class UnsupportedFault(ValueError):
    """The (category, action) pair in the plan has no Chaos Mesh mapping."""


def _num(value: Any) -> Any:
    """Render a numeric parameter without a spurious ``.0`` (250.0 -> 250)."""
    f = float(value)
    return int(f) if f.is_integer() else f


def _duration(seconds: float) -> str:
    return f"{int(round(seconds))}s"


def _selector(namespace: str, service_name: str) -> dict[str, Any]:
    return {"namespaces": [namespace], "labelSelectors": {"app": service_name}}


def _base(kind: str, name: str, namespace: str, experiment_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": CHAOS_API_VERSION,
        "kind": kind,
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {LABEL_KEY: experiment_id},
        },
        "spec": spec,
    }


def _apply_direction(spec: dict[str, Any], namespace: str, params: dict[str, Any]) -> None:
    """Scope a NetworkChaos to one upstream caller, when the plan names one.

    The frozen ExperimentPlan contract has no top-level caller field, so an
    optional caller is read from the free-form ``fault_profile.parameters``
    (``upstream_caller`` or ``caller``). Without one, the latency is applied to
    all of the target pod's traffic, which still models "this service is slow".
    """
    caller = params.get("upstream_caller") or params.get("caller")
    if caller:
        spec["direction"] = str(params.get("direction", "to"))
        spec["target"] = {"mode": "all", "selector": _selector(namespace, str(caller))}


def manifest_filename(experiment_id: str, kind: str) -> str:
    suffix = {"NetworkChaos": "net", "PodChaos": "pod", "StressChaos": "stress"}.get(kind, kind.lower())
    return f"{experiment_id}-{suffix}.yaml"


def build_manifest(plan: ExperimentPlan, *, duration_seconds: float, target_pods: int) -> tuple[str, dict[str, Any]]:
    """Return ``(kind, manifest_dict)`` for the plan's fault.

    ``target_pods`` is the number of pods to hit (fixed mode). The caller is
    responsible for clamping it so at least one replica is left untouched.
    """
    fp = plan.fault_profile
    tn = plan.target_node
    ns = tn.namespace
    eid = plan.experiment_id
    params: dict[str, Any] = dict(fp.parameters or {})
    value = str(max(1, int(target_pods)))
    selector = _selector(ns, tn.service_name)
    category = (fp.category or "").strip().lower()
    action = (fp.action or "").strip().lower()

    if action in _LATENCY_ACTIONS:
        delay: dict[str, Any] = {
            "latency": f"{_num(params.get('delay_ms', 0))}ms",
            "correlation": str(params.get("correlation", 0)),
        }
        if params.get("jitter_ms") is not None:
            delay["jitter"] = f"{_num(params['jitter_ms'])}ms"
        spec = {"action": "delay", "mode": "fixed", "value": value, "selector": selector, "delay": delay}
        _apply_direction(spec, ns, params)
        spec["duration"] = _duration(duration_seconds)
        return "NetworkChaos", _base("NetworkChaos", eid, ns, eid, spec)

    if action in _LOSS_ACTIONS:
        loss = {
            "loss": str(_num(params.get("loss_percent", params.get("loss", 50)))),
            "correlation": str(params.get("correlation", 0)),
        }
        spec = {"action": "loss", "mode": "fixed", "value": value, "selector": selector, "loss": loss}
        _apply_direction(spec, ns, params)
        spec["duration"] = _duration(duration_seconds)
        return "NetworkChaos", _base("NetworkChaos", eid, ns, eid, spec)

    if action in _KILL_ACTIONS:
        # pod-kill is instantaneous: Chaos Mesh deletes the pod once, so there is
        # no duration and no fault to roll back (the Deployment recreates the pod).
        spec = {"action": "pod-kill", "mode": "fixed", "value": value, "selector": selector}
        if params.get("grace_period") is not None:
            spec["gracePeriod"] = int(params["grace_period"])
        return "PodChaos", _base("PodChaos", eid, ns, eid, spec)

    if category == "stress" and action in _CPU_ACTIONS:
        stressors = {"cpu": {"workers": int(params.get("workers", 1)), "load": int(params.get("load", 80))}}
        spec = {"mode": "fixed", "value": value, "selector": selector,
                "stressors": stressors, "duration": _duration(duration_seconds)}
        return "StressChaos", _base("StressChaos", eid, ns, eid, spec)

    if category == "stress" and action in _MEM_ACTIONS:
        stressors = {"memory": {"workers": int(params.get("workers", 1)), "size": str(params.get("size", "256MB"))}}
        spec = {"mode": "fixed", "value": value, "selector": selector,
                "stressors": stressors, "duration": _duration(duration_seconds)}
        return "StressChaos", _base("StressChaos", eid, ns, eid, spec)

    raise UnsupportedFault(
        f"no Chaos Mesh mapping for category={fp.category!r} action={fp.action!r}. "
        "Supported: network/latency, network/loss, pod/pod-kill, stress/cpu, stress/memory."
    )


def describe_fault(kind: str, manifest: dict[str, Any]) -> str:
    """A short, human-readable line for the receipt's ``fault_applied`` field."""
    spec = manifest["spec"]
    app = spec["selector"]["labelSelectors"].get("app", "?")
    scope = f"{spec.get('value', '?')} pod(s) matching app={app}"
    if kind == "NetworkChaos" and spec.get("action") == "delay":
        d = spec["delay"]
        jitter = f" jitter={d['jitter']}" if "jitter" in d else ""
        return f"Chaos Mesh NetworkChaos delay latency={d['latency']}{jitter} on {scope}"
    if kind == "NetworkChaos" and spec.get("action") == "loss":
        return f"Chaos Mesh NetworkChaos loss {spec['loss']['loss']}% on {scope}"
    if kind == "PodChaos":
        return f"Chaos Mesh PodChaos pod-kill on {scope}"
    if kind == "StressChaos":
        which = "cpu" if "cpu" in spec.get("stressors", {}) else "memory"
        return f"Chaos Mesh StressChaos {which} on {scope}"
    return f"Chaos Mesh {kind} on {scope}"


def dump_manifest(manifest: dict[str, Any]) -> str:
    """Serialise a manifest to text. PyYAML if available, else JSON (valid YAML)."""
    try:
        import yaml  # type: ignore
        return yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    except ImportError:
        return json.dumps(manifest, indent=2) + "\n"


def write_manifest(path: Path | str, manifest: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_manifest(manifest), encoding="utf-8", newline="\n")
    return path
