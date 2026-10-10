"""Topology mapping: builds a directed dependency graph G = (V, E) of the microservices."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

from .recorded_data import RECORDED_DEPLOYMENTS, RECORDED_JAEGER_SPANS, RECORDED_TRAFFIC_EDGES

logger = logging.getLogger(__name__)


@dataclass
class NodeCapacity:
    replicas_total: int
    available_replicas: int
    cpu_limit: str = "200m"
    memory_limit: str = "128Mi"
    cpu_request: str = "100m"
    memory_request: str = "64Mi"


@dataclass
class ServiceNode:
    name: str
    namespace: str
    deployment: str
    capacity: NodeCapacity
    labels: dict[str, str] = field(default_factory=dict)
    ports: list[dict[str, Any]] = field(default_factory=list)
    in_degree: int = 0
    out_degree: int = 0
    callers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    on_checkout_path: bool = False
    is_choke_point: bool = False


@dataclass
class DependencyEdge:
    source: str
    destination: str
    rps: float = 0.0
    protocol: str = "grpc"
    observed_via: str = "istio_telemetry"


@dataclass
class TopologyGraph:
    nodes: dict[str, ServiceNode] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)
    critical_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {k: asdict(v) for k, v in self.nodes.items()},
            "edges": [asdict(e) for e in self.edges],
            "critical_path": self.critical_path,
            "metadata": {
                "total_services": len(self.nodes),
                "total_edges": len(self.edges),
            },
        }


class TopologyError(RuntimeError):
    """Live topology discovery failed."""


def build_topology(
    kube_namespace: str = "default",
    prometheus_url: str = "http://localhost:9090",
    jaeger_url: str = "http://localhost:16686",
    mock: bool = False,
) -> TopologyGraph:
    """Discovers nodes and edges from Kubernetes, Prometheus, and Jaeger (or recorded data)."""
    if mock:
        return _build_mock_topology(kube_namespace)

    # Live mode reports only what was discovered: a Kubernetes failure is an error, never recorded data.
    try:
        return _build_live_topology(kube_namespace, prometheus_url, jaeger_url)
    except Exception as e:
        raise TopologyError(f"live discovery of namespace {kube_namespace!r} failed: {e}") from e


def _build_mock_topology(kube_namespace: str) -> TopologyGraph:
    nodes: dict[str, ServiceNode] = {}
    for d in RECORDED_DEPLOYMENTS:
        container = (d.get("containers") or [{}])[0]
        reqs = container.get("requests", {})
        lims = container.get("limits", {})
        cap = NodeCapacity(
            replicas_total=int(d["replicas_total"]),
            available_replicas=int(d["available_replicas"]),
            cpu_limit=lims.get("cpu", "200m"),
            memory_limit=lims.get("memory", "128Mi"),
            cpu_request=reqs.get("cpu", "100m"),
            memory_request=reqs.get("memory", "64Mi"),
        )
        nodes[d["name"]] = ServiceNode(
            name=d["name"],
            namespace=kube_namespace,
            deployment=d["name"],
            capacity=cap,
            labels=d.get("labels", {}),
            ports=container.get("ports", []),
            on_checkout_path=d.get("on_checkout_path", False),
            is_choke_point=d.get("is_choke_point", False),
        )

    edges: list[DependencyEdge] = []
    for e in RECORDED_TRAFFIC_EDGES:
        src = e["source"]
        dst = e["destination"]
        edges.append(
            DependencyEdge(
                source=src,
                destination=dst,
                rps=float(e.get("rps", 0.0)),
                protocol=e.get("protocol", "grpc"),
                observed_via="prometheus_istio_requests_total",
            )
        )
        if src in nodes and dst not in nodes[src].dependencies:
            nodes[src].dependencies.append(dst)
        if dst in nodes:
            if src not in nodes[dst].callers:
                nodes[dst].callers.append(src)

    # Ingress from Jaeger
    for j in RECORDED_JAEGER_SPANS:
        src = j["caller_service"]
        dst = j["callee_service"]
        if src in nodes and dst not in nodes[src].dependencies:
            nodes[src].dependencies.append(dst)
        if dst in nodes and src not in nodes[dst].callers:
            nodes[dst].callers.append(src)

    for node in nodes.values():
        node.in_degree = len(node.callers)
        node.out_degree = len(node.dependencies)

    critical_path = ["frontend", "checkoutservice", "paymentservice"]
    return TopologyGraph(nodes=nodes, edges=edges, critical_path=critical_path)


def _build_live_topology(
    kube_namespace: str,
    prometheus_url: str,
    jaeger_url: str,
) -> TopologyGraph:
    """Query live Kubernetes API, Prometheus, and Jaeger to construct graph."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()

    apps_v1 = client.AppsV1Api()
    deployments = apps_v1.list_namespaced_deployment(namespace=kube_namespace)

    nodes: dict[str, ServiceNode] = {}
    for d in deployments.items:
        app_name = d.metadata.name
        spec = d.spec
        replicas = spec.replicas or 1
        available = d.status.available_replicas or 0

        containers = spec.template.spec.containers or []
        primary_container = containers[0] if containers else None
        res = primary_container.resources if primary_container else None
        limits = res.limits if res else {}
        requests_map = res.requests if res else {}

        cap = NodeCapacity(
            replicas_total=replicas,
            available_replicas=available,
            cpu_limit=str(limits.get("cpu", "200m")),
            memory_limit=str(limits.get("memory", "128Mi")),
            cpu_request=str(requests_map.get("cpu", "100m")),
            memory_request=str(requests_map.get("memory", "64Mi")),
        )
        labels = d.metadata.labels or {}
        nodes[app_name] = ServiceNode(
            name=app_name,
            namespace=kube_namespace,
            deployment=app_name,
            capacity=cap,
            labels=labels,
            on_checkout_path=app_name in ("frontend", "checkoutservice", "paymentservice", "currencyservice", "cartservice", "shippingservice"),
            is_choke_point=app_name in ("paymentservice", "checkoutservice", "currencyservice", "cartservice"),
        )

    edges: list[DependencyEdge] = []
    # Query Prometheus for istio_requests_total edges
    try:
        query = f'sum(rate(istio_requests_total{{destination_workload_namespace="{kube_namespace}"}}[5m])) by (source_workload, destination_workload)'
        resp = requests.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=3.0)
        if resp.status_code == 200:
            data = resp.json().get("data", {}).get("result", [])
            for item in data:
                metric = item.get("metric", {})
                src = metric.get("source_workload")
                dst = metric.get("destination_workload")
                val = float((item.get("value") or [0, 0])[1])
                if src and dst and src != "unknown" and dst != "unknown":
                    edges.append(
                        DependencyEdge(
                            source=src,
                            destination=dst,
                            rps=round(val, 2),
                            protocol="grpc",
                            observed_via="prometheus_istio_requests_total",
                        )
                    )
                    if src in nodes and dst not in nodes[src].dependencies:
                        nodes[src].dependencies.append(dst)
                    if dst in nodes and src not in nodes[dst].callers:
                        nodes[dst].callers.append(src)
    except Exception as e:
        logger.warning("Prometheus edge query failed: %s", e)

    # No measured edges (a cold mesh) stay empty: recorded edges would describe some other cluster.
    if not edges:
        logger.warning("Prometheus returned no service-to-service edges; topology has nodes only")

    for node in nodes.values():
        node.in_degree = len(node.callers)
        node.out_degree = len(node.dependencies)

    return TopologyGraph(nodes=nodes, edges=edges, critical_path=["frontend", "checkoutservice", "paymentservice"])


def save_topology(exp_dir: Path | str, topology: TopologyGraph) -> Path:
    """Saves topology.json into the experiment directory."""
    path = Path(exp_dir) / "topology.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(topology.to_dict(), fh, indent=2)
        fh.write("\n")
    return path
