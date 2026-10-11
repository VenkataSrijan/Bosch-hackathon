"""Target and fault profile selection based on graph centrality, redundancy, and LLM hypothesis."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from common.contracts import FaultProfile, TargetNode
from common.llm import LLMUnavailable, complete

from .topology import ServiceNode, TopologyGraph

logger = logging.getLogger(__name__)


@dataclass
class TargetSelection:
    target_node: TargetNode
    fault_profile: FaultProfile
    hypothesis: str
    ranking_score: float
    candidate_summary: list[dict[str, Any]]


# Faults an operator can ask for, with their Chaos Mesh category and default parameters.
# These are the kinds Phase 2 can synthesise and Phase 3 can remove on abort (NetworkChaos), plus
# pod-kill, which is instantaneous and leaves nothing to remove.
FAULTS: dict[str, tuple[str, dict[str, Any]]] = {
    "latency": ("network", {"delay_ms": 250, "jitter_ms": 20}),
    "loss": ("network", {"loss_percent": 30}),
    "pod-kill": ("pod", {}),
    "cpu": ("stress", {"workers": 2, "load": 80}),
    "memory": ("stress", {"workers": 1, "size_mb": 256}),
    "dependency": ("network", {}),  # partition the target from one caller; Phase 1 picks the caller
}


def fault_parameters(action: str, overrides: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """(category, parameters) for `action`, with operator overrides validated."""
    if action not in FAULTS:
        raise ValueError(f"unsupported fault {action!r}; choose one of: {', '.join(FAULTS)}")
    category, defaults = FAULTS[action]
    params = dict(defaults)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key not in defaults:
            raise ValueError(f"fault {action!r} takes no parameter {key!r}")
        params[key] = value
    if action == "latency" and not (1 <= params["delay_ms"] <= 5000 and 0 <= params["jitter_ms"] <= params["delay_ms"]):
        raise ValueError("latency needs 1 <= delay_ms <= 5000 and 0 <= jitter_ms <= delay_ms")
    if action == "loss" and not (1 <= params["loss_percent"] <= 100):
        raise ValueError("loss needs 1 <= loss_percent <= 100")
    if action == "cpu" and not (1 <= params["workers"] <= 8 and 1 <= params["load"] <= 100):
        raise ValueError("cpu stress needs 1 <= workers <= 8 and 1 <= load <= 100")
    if action == "memory" and not (1 <= params["workers"] <= 8 and 16 <= params["size_mb"] <= 4096):
        raise ValueError("memory stress needs 1 <= workers <= 8 and 16 <= size_mb <= 4096")
    return category, params


def select_target_and_fault(
    topology: TopologyGraph,
    category: str = "network",
    action: str = "latency",
    delay_ms: int = 250,
    jitter_ms: int = 20,
    max_duration_seconds: int = 60,
    target: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> TargetSelection:
    """Ranks services and picks the most critical target with >= 2 replicas.

    `target` forces the service (it must still have >= 2 replicas); `parameters` are the fault's own
    parameters (see FAULTS) and replace delay_ms / jitter_ms when given.
    """
    candidates: list[tuple[float, ServiceNode]] = []
    summary: list[dict[str, Any]] = []

    for name, node in topology.nodes.items():
        reps = node.capacity.replicas_total
        # Fixed Rule: Never select a service with fewer than 2 replicas
        if reps < 2:
            summary.append({
                "service": name,
                "replicas": reps,
                "status": "ineligible_insufficient_replicas",
            })
            continue

        # Score based on checkout path position, in-degree (fan-in), and choke-point status
        score = 0.0
        if node.on_checkout_path:
            score += 50.0
        if node.is_choke_point:
            score += 30.0
        score += float(node.in_degree) * 10.0

        candidates.append((score, node))
        summary.append({
            "service": name,
            "replicas": reps,
            "score": score,
            "status": "eligible",
        })

    if not candidates:
        raise ValueError(
            "No service has at least 2 replicas; cannot safely target any node. "
            "Scale the target deployment (e.g. `kubectl scale deployment paymentservice --replicas=2`)."
        )

    # Sort descending by score
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_score, chosen_node = candidates[0]
    if target is not None:
        if target not in topology.nodes:
            raise ValueError(f"target {target!r} is not a deployment in this namespace "
                             f"(found: {', '.join(sorted(topology.nodes))})")
        chosen = [c for c in candidates if c[1].name == target]
        if not chosen:
            raise ValueError(
                f"target {target!r} has {topology.nodes[target].capacity.replicas_total} replica(s); at least 2 are "
                f"needed so one stays untouched (kubectl scale deployment {target} --replicas=2)")
        best_score, chosen_node = chosen[0]

    # targetable_replicas = replicas_total - 1 at most
    targetable = max(1, min(1, chosen_node.capacity.replicas_total - 1))

    target = TargetNode(
        service_name=chosen_node.name,
        namespace=chosen_node.namespace,
        deployment=chosen_node.deployment,
        replicas_total=chosen_node.capacity.replicas_total,
        targetable_replicas=targetable,
    )

    if action == "dependency":
        # Sever the target from one of its callers: from that caller's side the target is a
        # failed dependency. Blast-safe because we still hit only one target replica.
        category = "network"
        caller = (parameters or {}).get("caller") or (parameters or {}).get("dependency")
        if not caller:
            caller = chosen_node.callers[0] if chosen_node.callers else None
        if not caller:
            raise ValueError(
                f"dependency failure needs an upstream caller to cut, but {chosen_node.name} has no "
                "observed callers; choose a service that something else calls")
        fault_params: dict[str, Any] = {"caller": caller, "direction": "both"}
    elif parameters is None:
        fault_params = {"delay_ms": delay_ms, "jitter_ms": jitter_ms}
    else:
        category, fault_params = fault_parameters(action, parameters)

    fault = FaultProfile(
        category=category,
        action=action,
        parameters={**fault_params, "target_pods": targetable},
        ramp_strategy="canary_stepped",
        max_duration_seconds=max_duration_seconds,
    )

    hypothesis = _generate_hypothesis(chosen_node, fault)

    return TargetSelection(
        target_node=target,
        fault_profile=fault,
        hypothesis=hypothesis,
        ranking_score=best_score,
        candidate_summary=summary,
    )


def _generate_hypothesis(node: ServiceNode, fault: FaultProfile) -> str:
    """Generates an experiment hypothesis using LLM if available, else deterministic template."""
    system = "You are a Red Agent chaos engineering architect. Formulate a 1-sentence falsifiable resilience hypothesis."
    what = describe_fault(fault)
    user = (
        f"Target: {node.name} (replicas: {node.capacity.replicas_total}, callers: {', '.join(node.callers) or 'ingress'}). "
        f"Injected fault: {what} on {fault.parameters.get('target_pods')} replica. "
        f"Hypothesis on upstream resilience and circuit breaking:"
    )

    try:
        res = complete(system=system, user=user)
        if res and len(res.strip()) > 10:
            return res.strip()
    except (LLMUnavailable, Exception) as e:
        logger.info("LLM unavailable (%s); using deterministic hypothesis", e)

    return (
        f"Injecting {what} into {fault.parameters.get('target_pods')} of {node.capacity.replicas_total} "
        f"{node.name} replicas tests whether upstream callers isolate the failure or suffer a cascading collapse."
    )


def describe_fault(fault: FaultProfile) -> str:
    p = fault.parameters
    if fault.action == "latency":
        return f"{p.get('delay_ms')}ms latency ({p.get('jitter_ms')}ms jitter)"
    if fault.action == "loss":
        return f"{p.get('loss_percent')}% packet loss"
    if fault.action == "pod-kill":
        return "a pod kill"
    if fault.action == "cpu":
        return f"CPU stress ({p.get('workers')} workers at {p.get('load')}% load)"
    if fault.action == "memory":
        return f"memory stress ({p.get('workers')} worker(s), {p.get('size_mb')} MB each)"
    if fault.action == "dependency":
        return f"dependency failure (network partition from {p.get('caller')})"
    return f"{fault.category}/{fault.action}"
