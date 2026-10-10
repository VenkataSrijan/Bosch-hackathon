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


def select_target_and_fault(
    topology: TopologyGraph,
    category: str = "network",
    action: str = "latency",
    delay_ms: int = 250,
    jitter_ms: int = 20,
    max_duration_seconds: int = 60,
) -> TargetSelection:
    """Ranks services and picks the most critical target with >= 2 replicas."""
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

    # targetable_replicas = replicas_total - 1 at most
    targetable = max(1, min(1, chosen_node.capacity.replicas_total - 1))

    target = TargetNode(
        service_name=chosen_node.name,
        namespace=chosen_node.namespace,
        deployment=chosen_node.deployment,
        replicas_total=chosen_node.capacity.replicas_total,
        targetable_replicas=targetable,
    )

    fault = FaultProfile(
        category=category,
        action=action,
        parameters={
            "delay_ms": delay_ms,
            "jitter_ms": jitter_ms,
            "target_pods": targetable,
        },
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
    user = (
        f"Target: {node.name} (replicas: {node.capacity.replicas_total}, callers: {', '.join(node.callers) or 'ingress'}). "
        f"Injected Fault: {fault.parameters.get('delay_ms')}ms delay ({fault.parameters.get('jitter_ms')}ms jitter) on {fault.parameters.get('target_pods')} replica. "
        f"Hypothesis on upstream resilience and circuit breaking:"
    )

    try:
        res = complete(system=system, user=user)
        if res and len(res.strip()) > 10:
            return res.strip()
    except (LLMUnavailable, Exception) as e:
        logger.info("LLM unavailable (%s); using deterministic hypothesis", e)

    return (
        f"Injecting {fault.parameters.get('delay_ms')}ms latency into {fault.parameters.get('target_pods')} of {node.capacity.replicas_total} "
        f"{node.name} replicas tests whether upstream callers isolate socket delays or suffer cascading timeout collapse."
    )
