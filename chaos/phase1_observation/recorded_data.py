"""Recorded high-fidelity topology, capacity, and telemetry for mock/offline execution."""
from __future__ import annotations

from typing import Any

# Recorded Kubernetes Deployments for Google Online Boutique in namespace "default"
# Note: paymentservice has 2 replicas (scaled per hackathon cluster setup).
RECORDED_DEPLOYMENTS: list[dict[str, Any]] = [
    {
        "name": "paymentservice",
        "namespace": "default",
        "replicas_total": 2,
        "available_replicas": 2,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/paymentservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 50051}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "paymentservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": True,
    },
    {
        "name": "checkoutservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/checkoutservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 5050}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "checkoutservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": True,
    },
    {
        "name": "frontend",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/frontend:v0.10.4",
                "ports": [{"name": "http", "containerPort": 8080}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "frontend", "tier": "frontend"},
        "on_checkout_path": True,
        "is_choke_point": False,
    },
    {
        "name": "currencyservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/currencyservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 7000}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "currencyservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": True,
    },
    {
        "name": "cartservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/cartservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 7070}],
                "requests": {"cpu": "200m", "memory": "64Mi"},
                "limits": {"cpu": "300m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "cartservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": True,
    },
    {
        "name": "shippingservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/shippingservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 50051}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "shippingservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": False,
    },
    {
        "name": "productcatalogservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/productcatalogservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 3550}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "productcatalogservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": True,
    },
    {
        "name": "recommendationservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/recommendationservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 8080}],
                "requests": {"cpu": "100m", "memory": "220Mi"},
                "limits": {"cpu": "200m", "memory": "450Mi"},
            }
        ],
        "labels": {"app": "recommendationservice", "tier": "backend"},
        "on_checkout_path": False,
        "is_choke_point": False,
    },
    {
        "name": "adservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/adservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 9555}],
                "requests": {"cpu": "200m", "memory": "180Mi"},
                "limits": {"cpu": "300m", "memory": "300Mi"},
            }
        ],
        "labels": {"app": "adservice", "tier": "backend"},
        "on_checkout_path": False,
        "is_choke_point": False,
    },
    {
        "name": "emailservice",
        "namespace": "default",
        "replicas_total": 1,
        "available_replicas": 1,
        "containers": [
            {
                "name": "server",
                "image": "gcr.io/google-samples/microservices-demo/emailservice:v0.10.4",
                "ports": [{"name": "grpc", "containerPort": 8080}],
                "requests": {"cpu": "100m", "memory": "64Mi"},
                "limits": {"cpu": "200m", "memory": "128Mi"},
            }
        ],
        "labels": {"app": "emailservice", "tier": "backend"},
        "on_checkout_path": True,
        "is_choke_point": False,
    },
]

# Recorded Istio / Prometheus Traffic Flow Edges (source_workload -> destination_workload)
RECORDED_TRAFFIC_EDGES: list[dict[str, Any]] = [
    {"source": "frontend", "destination": "checkoutservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "frontend", "destination": "productcatalogservice", "rps": 38.6, "protocol": "grpc"},
    {"source": "frontend", "destination": "currencyservice", "rps": 25.1, "protocol": "grpc"},
    {"source": "frontend", "destination": "cartservice", "rps": 19.3, "protocol": "grpc"},
    {"source": "frontend", "destination": "recommendationservice", "rps": 12.0, "protocol": "grpc"},
    {"source": "frontend", "destination": "shippingservice", "rps": 8.5, "protocol": "grpc"},
    {"source": "frontend", "destination": "adservice", "rps": 12.0, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "paymentservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "cartservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "currencyservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "shippingservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "productcatalogservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "checkoutservice", "destination": "emailservice", "rps": 12.4, "protocol": "grpc"},
    {"source": "recommendationservice", "destination": "productcatalogservice", "rps": 12.0, "protocol": "grpc"},
]

# Recorded Jaeger Tracing Client/Server Span Pairs
RECORDED_JAEGER_SPANS: list[dict[str, Any]] = [
    {
        "trace_id": "8f8a10bc39e24a1b",
        "caller_service": "frontend",
        "caller_operation": "HTTP POST /cart/checkout",
        "callee_service": "checkoutservice",
        "callee_operation": "hipstershop.CheckoutService/PlaceOrder",
        "duration_ms": 24.5,
    },
    {
        "trace_id": "8f8a10bc39e24a1b",
        "caller_service": "checkoutservice",
        "caller_operation": "hipstershop.CheckoutService/PlaceOrder",
        "callee_service": "paymentservice",
        "callee_operation": "hipstershop.PaymentService/Charge",
        "duration_ms": 8.2,
    },
]

# Recorded Steady-State Telemetry Profile (Rolling 15-minute window)
RECORDED_PROFILING: dict[str, Any] = {
    "steady_state_baseline": {
        "p99_latency_ms": 18.5,
        "global_error_rate_percent": 0.02,
        "checkout_success_rate_percent": 99.98,
    },
    "services": {
        "paymentservice": {
            "p50_latency_ms": 4.2,
            "p90_latency_ms": 9.8,
            "p99_latency_ms": 15.6,
            "traffic_rps": 12.4,
            "error_ratio_percent": 0.01,
            "cpu_saturation_percent": 18.4,
            "memory_saturation_percent": 42.1,
        },
        "checkoutservice": {
            "p50_latency_ms": 8.6,
            "p90_latency_ms": 14.1,
            "p99_latency_ms": 18.5,
            "traffic_rps": 12.4,
            "error_ratio_percent": 0.02,
            "cpu_saturation_percent": 24.0,
            "memory_saturation_percent": 48.5,
        },
        "currencyservice": {
            "p50_latency_ms": 2.1,
            "p90_latency_ms": 5.4,
            "p99_latency_ms": 9.2,
            "traffic_rps": 37.5,
            "error_ratio_percent": 0.0,
            "cpu_saturation_percent": 15.2,
            "memory_saturation_percent": 36.8,
        },
        "cartservice": {
            "p50_latency_ms": 3.8,
            "p90_latency_ms": 7.5,
            "p99_latency_ms": 12.1,
            "traffic_rps": 31.7,
            "error_ratio_percent": 0.0,
            "cpu_saturation_percent": 21.6,
            "memory_saturation_percent": 55.4,
        },
    },
}
