# Phase 1: Observation and Dynamic Profiling (Red Agent)

**Owner:** Pardhiv  
**Role:** Red Agent (Adversary Observer)  
**Contract Output:** `ExperimentPlan.json`, `topology.json`

---

## 1. Overview

Phase 1 operates strictly read-only against the cluster and telemetry systems. It is responsible for discovering the current system topology, profiling the steady-state baseline, computing the blast-radius error budget, and formulating a mathematically bounded `ExperimentPlan.json`.

### Core Deliverables:
1. **Topology Mapping ($G = (V, E)$):** Discovers all Kubernetes Deployments, Services, replica counts, and container capacity limits. Maps directed communication edges via Istio Prometheus metrics (`istio_requests_total`) and Jaeger client/server trace spans. Saved to `topology.json`.
2. **Steady-State Profiling:** Profiles a rolling time window (default 15 minutes, configurable down to 2 minutes) measuring p50/p90/p99 latency, traffic in RPS, 5xx error ratios, and CPU/memory saturation.
3. **Blast-Radius Budget:** Implements the mathematical error budget formula:
   $$\text{Max Allowable Degradation} = \text{Baseline Error Rate} + \text{Remaining Error Budget} \times \alpha$$
   where $\alpha = \text{SAFETY\_ALPHA}$ (default 0.10).
4. **Target Selection with N-1 Redundancy:** Ranks candidate services by critical checkout path position, in-degree (fan-in), and choke-point status. **Strictly enforces that targetable replicas $\le$ replicas_total - 1** and never selects a service with fewer than 2 replicas.
5. **Dynamic Abort Thresholds:** Derives thresholds where `max_p99_latency_ms` strictly exceeds $\text{delay\_ms} + \text{jitter\_ms}$, ensuring aborts trigger only upon true cascading amplification.
6. **Artifact Output:** Writes `ExperimentPlan.json` with a validated `exp-<app>-<target>-<3 digits>` identifier.

---

## 2. How to Run

### Standalone (Mock Mode - No Cluster or API Keys Needed)
```bash
python -m phase1_observation --out artifacts/mock --mock
```
Output:
* Writes `artifacts/mock/<experiment_id>/ExperimentPlan.json`
* Writes `artifacts/mock/<experiment_id>/topology.json`
* Emits the created experiment folder path as the **last line of stdout**.

### Standalone (Live Cluster Mode)
Requires a running Kubernetes cluster with Online Boutique and Istio/Prometheus deployed:
```bash
export KUBE_NAMESPACE="default"
export PROMETHEUS_URL="http://localhost:9090"
export JAEGER_URL="http://localhost:16686"
python -m phase1_observation --out artifacts --window 15
```

### Full Pipeline Integration
```bash
python run_pipeline.py --mock
```

---

## 3. Configuration

Environment variables (with defaults in `common/config.py`):
| Variable | Default | Description |
|---|---|---|
| `KUBE_NAMESPACE` | `default` | Target Kubernetes namespace |
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus endpoint for Istio metrics |
| `JAEGER_URL` | `http://localhost:16686` | Jaeger query endpoint for trace spans |
| `ARTIFACTS_DIR` | `artifacts` | Root directory for experiment dossiers |
| `SLO_CHECKOUT_SUCCESS_PERCENT` | `99.5` | Target checkout success SLO |
| `SAFETY_ALPHA` | `0.10` | Fractional share of remaining error budget allocated |

---

## 4. RBAC Manifest (Read-Only)

Phase 1 runs under a strictly read-only ServiceAccount:
```bash
kubectl apply -f deploy/rbac/red-observer.yaml
```
Grants `get`, `list`, `watch` on Deployments, Services, Endpoints, Pods, ConfigMaps, and HPAs. Does not have write, create, or delete permissions anywhere on the cluster.

---

## 5. Testing

Run pytest from the `chaos/` directory:
```bash
pytest tests/phase1/
```
Tests cover:
* Main mock path generating fully contract-compliant `ExperimentPlan.json` and `topology.json`.
* Failure path: verifying `ValueError` when no candidate service meets the 2-replica minimum.
* Mathematical validation of the blast-radius degradation budget and latency thresholds.
* CLI contract verifying the last line of stdout is the artifact folder path.

---

## 6. Known Gaps & Stretch Goals

* **Completed:** Full dynamic topology builder, rolling Prometheus profiler, blast radius budget calculator, target selector with redundancy guards, and LLM hypothesis generation with deterministic fallback.
* **Stretch Work:** Integration of eBPF socket-level discovery for un-instrumented outbound external APIs, and auto-parsing Horizontal Pod Autoscaler (HPA) metrics in live clusters.
