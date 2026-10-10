# Phase 3: Diagnostic Judge and Abort Switch (Blue Agent)

Watches the experiment, stops it the moment it breaches the plan's safety limits, and explains what happened
from traces and logs. Never applies faults; never writes `InjectionReceipt.json`.

```bash
cd chaos
python -m phase3_judge --exp artifacts/<experiment_id>                         # live
python -m phase3_judge --exp artifacts/mock/<experiment_id> --mock [--mock-scenario abort|healthy]
```

`run_pipeline.py` starts it in the background before Phase 2.

## What it does

1. **Waits** up to 120 s for `InjectionReceipt.json`.
2. **Samples edge health every second** and appends it to `HealthSeries.json`. Live queries (Prometheus,
   `PROMETHEUS_URL`):
   - error rate % = `(sum(rate(istio_requests_total{destination_workload="frontend",response_code=~"5.."}[30s])) or vector(0)) / sum(rate(istio_requests_total{destination_workload="frontend"}[30s])) * 100`
   - checkout success rate % = 100 - error rate
   - p99 = `histogram_quantile(0.99, sum by (le) (rate(istio_request_duration_milliseconds_bucket{destination_workload="frontend",reporter="destination"}[30s])))`

   `or vector(0)` keeps the ratio defined while no 5xx series exists yet (a healthy system). With no traffic
   at all the result is empty and the sample is missing.
3. **Missing data is never filled in.** A query that fails or returns no number (empty, NaN) makes the sample
   missing: it is not appended to `HealthSeries.json`, it is logged in `JudgeLog.json`, and
   **3 missing samples in a row trigger an abort** (fail safe).
4. **Checks all three limits from the plan's `abort_thresholds`** on every sample: `max_p99_latency_ms`,
   `max_error_rate_percent`, `min_checkout_success_rate_percent`. The global `SLO_CHECKOUT_SUCCESS_PERCENT` is
   not used.
5. **Canary verdict**: at the end of the burst (`initiated_at + CANARY_SECONDS`) it writes `CanaryVerdict.json`:
   GO if the burst's samples stayed inside all limits, NO_GO if there was a breach or no valid sample.
6. **Emergency abort**, in this order:
   1. `kubectl delete networkchaos -n <namespace> -l chaos.ai/experiment-id=<experiment_id> --now`
   2. `AbortSignal.json`
   3. `../QUARANTINE.json`, whose reason says whether the delete succeeded.

   For a no-data abort the contract still needs a number, so `observed_value` is `-1`. The reason in
   `QUARANTINE.json` says "no data".
7. After the receipt is final it keeps sampling for **30 s of recovery**, then writes `RootCauseAnalysis.json`.
   - **ABORT_TRIGGERED**: it aborted.
   - **DEGRADED_NO_ABORT**: some sample used up at least half of the margin between the plan's baseline and a
     limit.
   - **STEADY_STATE_MAINTAINED**: otherwise.

   `impact_summary` comes from the measured samples. If there are none, it uses the plan's measured baseline,
   and `JudgeLog.json` says so.

## Root cause (`evidence.py`)

Only for ABORT_TRIGGERED and DEGRADED_NO_ABORT. The window is [injection - 30 s, end + 30 s].

- **Traces.** Jaeger (`JAEGER_URL`, `/api/traces` per service).
  - Every RPC becomes a call: caller → callee, with duration and failure status (gRPC status, HTTP 5xx, Envoy
    response flags).
  - The call is taken from the caller's client span, which is what the caller waited for. With netem delay,
    the callee's own server span often looks fast.
  - It works whether or not the services forward trace context; Online Boutique's apps propagate W3C context
    while Istio's Zipkin tracer uses B3, so each RPC may end up as its own trace.
- **Slow or failing edge.** Calls are split into baseline (before injection) and fault (injection to end).
  - An edge is slow if its fault-window p95 exceeds max(2 × baseline p95, baseline p95 + 25 ms).
  - An edge is failing if at least 2 calls failed and its failure rate rose by more than 1 point.
- **`cascade_path`.** Starts at the injected service. Each hop is the caller with the most slow or failing calls
  into the previous hop. `loadgenerator` is never a hop. Each `manifestation` holds the measured numbers and the
  pod-log error count.
- **`root_cause_isolation`.**
  - `faulty_service`: the injected service's direct caller in the cascade.
  - `mechanism`: built from that edge. Timeouts but an error passed upstream means no fallback. Failures with
    nothing hiding them means no retry or fallback. Slow calls never cut short mean no deadline.
  - `stack_trace_snippet`: up to 3 real error lines from `kubectl logs` of the cascade's pods, or null.
- **No evidence.** When Jaeger is unreachable or shows nothing, the fields say so. The hop says "no Jaeger
  traces …", the mechanism says "No evidence of propagation found … Root cause not isolated.", and the snippet is
  null.
- **No hardcoded text or line numbers.** No service name, file or line number is hardcoded.

## Files written

| File | |
|---|---|
| `HealthSeries.json`, `CanaryVerdict.json`, `AbortSignal.json`, `RootCauseAnalysis.json`, `../QUARANTINE.json` | contracts |
| `JudgeLog.json` | Phase 3's own record: missing samples, canary summary, abort action, evidence notes and per-edge stats |

## Mock mode

`--mock` replays `phase3_judge/recorded/` on a virtual clock:
- `health_abort.json` / `health_healthy.json`: Phase 3's replay format.
- `jaeger_traces.json`: the Jaeger API format.
- `pod_logs.json`: the `kubectl logs --timestamps` format.

**The recordings are synthetic**, generated to look like a 250 ms delay on one of two paymentservice replicas.
They run through exactly the same threshold, canary and evidence code as live data.

- `abort` (default): the canary stays inside the limits (GO), then p99 crosses 500 ms after the fault is
  re-applied, and the judge aborts.
- `healthy`: nothing approaches a limit, so the verdict is STEADY_STATE_MAINTAINED.
- Both recordings have one missing sample, to show that it is logged and not filled in.

In mock, the abort skips kubectl and says so.

## Permissions

`chaos/deploy/rbac/blue-agent.yaml` grants:
- get/list on pods and pods/log
- list/delete on networkchaos, because `kubectl delete -l` lists before it deletes

Nothing else.

## Tests

```bash
cd chaos && pytest tests/phase3
```

## Known gaps

- Not yet run against a live cluster.
- The live Jaeger and log paths are tested with fakes only.
- Only `networkchaos` is deleted on abort, as specified. Phase 2 can also create podchaos/stresschaos. Those
  still carry a Chaos Mesh `duration` and are removed by Phase 2 when it sees `AbortSignal.json`.
- No LLM and no anomaly detection beyond the fixed thresholds (both stretch goals).
