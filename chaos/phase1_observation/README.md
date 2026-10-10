# Phase 1: Observation and Dynamic Profiling (Red Agent)

**Owner:** Pardhiv  
**Role:** Red Agent (Adversary Observer)  
**Contract Output:** `ExperimentPlan.json`, `topology.json`

---

## 1. Overview

Phase 1 operates strictly read-only against the cluster and telemetry systems. It is responsible for discovering the current system topology, profiling the steady-state baseline, computing the blast-radius error budget, and formulating a mathematically bounded `ExperimentPlan.json`.

### Core Deliverables:
1. **Topology Mapping ($G = (V, E)$):** Discovers all Kubernetes Deployments, Services, replica counts, and container capacity limits. Maps directed communication edges via Istio Prometheus metrics (`istio_requests_total`) and Jaeger client/server trace spans. Saved to `topology.json`.
2. **Steady-State Profiling:** Profiles a rolling time window (default 15 minutes, configurable down to 2 minutes). The baseline is measured at the frontend edge with the same definitions the Phase 3 judge checks against the abort limits: frontend p99 (`reporter="destination"`), frontend 5xx error rate, and checkout success = 100 − error rate. Per-service p50/p90/p99, RPS and error ratio go to `observation_metadata.json`; a value Prometheus does not return stays `null`, and CPU/memory saturation is not measured.
   **Live mode reports only what was measured.** If the baseline cannot be measured (Prometheus unreachable, no data, NaN) or Kubernetes discovery fails, Phase 1 exits 1 with the reason. It never falls back to recorded or default numbers. With no measured service-to-service edges, `topology.json` has nodes only.
3. **Blast-Radius Budget:** Implements the mathematical error budget formula:
   $$\text{Max Allowable Degradation} = \text{Baseline Error Rate} + \text{Remaining Error Budget} \times \alpha$$
   where $\alpha = \text{SAFETY\_ALPHA}$ (default 0.10).
4. **Target Selection with N-1 Redundancy:** Ranks candidate services by critical checkout path position, in-degree (fan-in), and choke-point status. **Strictly enforces that targetable replicas $\le$ replicas_total - 1** and never selects a service with fewer than 2 replicas. An operator can name the target and the fault instead (see below); the replica rule still applies.
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
export JAEGER_URL="http://localhost:16686/jaeger"
python -m phase1_observation --out artifacts --window 15
```

### Choosing the target and the fault
```bash
python -m phase1_observation --out artifacts --target paymentservice --fault latency --delay-ms 400 --jitter-ms 50
python -m phase1_observation --out artifacts --target paymentservice --fault loss --loss-percent 30 --duration 90
python -m phase1_observation --out artifacts --target paymentservice --fault pod-kill
python -m phase1_observation --out artifacts --delay-ms 250 --max-p99-ms 300      # tighter p99 abort limit
```
| Option | Default | |
|---|---|---|
| `--target` | highest-impact service | must have at least 2 replicas |
| `--fault` | `latency` | `latency`, `loss` or `pod-kill` |
| `--delay-ms` / `--jitter-ms` | 250 / 20 | latency only |
| `--loss-percent` | 30 | loss only |
| `--duration` | 60 | fault window, 10–600 s |
| `--max-p99-ms` | derived from the baseline | must exceed delay + jitter for latency |

`run_pipeline.py` and the operations console pass the same options through.

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
| `JAEGER_URL` | `http://localhost:16686/jaeger` | Jaeger query endpoint (the Istio addon serves it under `/jaeger`) |
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
