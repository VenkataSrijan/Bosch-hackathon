# Phase 4: Remediation Advisor + Operations Console (Blue Agent)

Turns `RootCauseAnalysis.json` into a concrete, reviewable fix, and shows the whole experiment on one screen.
The advisor never changes application code: the patch is a suggestion, checked with `git apply --check` and
never applied.

## Advisor

```bash
cd chaos
python -m phase4_advisor --exp artifacts/<experiment_id> --repo .. [--mock]
```

Inputs: `ExperimentPlan.json` and `RootCauseAnalysis.json` in the experiment folder, plus the repo's source.
Outputs, in the experiment folder:

| File | What |
|---|---|
| `Recommendation.json` | the contract artifact |
| `ResilienceAudit.json` | Phase 4's own record: the localized call site, each control present/missing with evidence, how the patch was made |
| `Recommendation.patch` | the suggested diff as a file, for `git apply` after review |

How it works:

1. **Localization** (`localize.py`). The dependency the faulty service waits on is the hop before it in
   `cascade_path` (else the plan's target). The advisor searches `src/<faulty_service>/` for real client calls
   to that dependency: Go `pb.New<X>Client(conn).<Method>(ctx, ...)` (including calls split across lines) and
   Python `<X>Stub` calls. Other languages get a name match only. Line numbers in the RCA are never used; method
   names, file names and symbols mentioned in the RCA only rank multiple candidates.
2. **Gap audit** (`audit.py`). For the call site: client-side deadline, retry with backoff, circuit breaker,
   fallback path. Each is looked for in the calling function, in the service's client setup (retry/breaker
   libraries) and in Istio `VirtualService` / `DestinationRule` manifests in the repo.
3. **Patch** (`patches.py`). Missing deadline: a per-call `context.WithTimeout` (Go) or `timeout=` (Python).
   Missing retry or breaker: a new `istio-manifests/<dependency>-resilience.yaml` with retries + per-try timeout
   and outlier detection. With `LLM_PROVIDER` set (and no `--mock`), the LLM drafts the code part first; its
   draft is used only if it passes `git apply --check`, otherwise the template is used.
   The deadline is `max(100 ms, 8 x baseline edge p99)` rounded up to 50 ms, capped at half the latency abort
   threshold (150 ms for the sample plan).
4. **Priority**: `ABORT_TRIGGERED` → HIGH, `DEGRADED_NO_ABORT` → MEDIUM (LOW if only retry/fallback are
   missing), `STEADY_STATE_MAINTAINED` → INFO with `suggested_diff: null`.

`--mock` disables the LLM. Everything else (the source search, the audit, `git apply --check`) is the same in
mock and live, because it only reads this repo.

Needs: Python 3.11+, `git` on PATH, `PyYAML` (`phase4_advisor/requirements.txt`).

## Console

```bash
cd chaos
python -m console                      # live: reads artifacts/, injects into the cluster in the current kube context
python -m console --mock               # mock: reads artifacts/mock, injections run with --mock
python -m console --artifacts <dir> [--port 8080] [--host 127.0.0.1]
```

Standard library only. The experiment views read only the artifacts folder. The **Inject a fault** panel starts
`run_pipeline.py` with the chosen target and fault, so every safety rule of the phases still applies (two
replicas, canary, abort limits, quarantine).

**Inject a fault.**
- **Choices:** a target service (only those with at least 2 replicas can be picked), a fault, its parameters,
  the fault window, and optionally a tighter p99 abort limit.
  - Network latency: delay and jitter.
  - Packet loss: percent dropped.
  - Pod kill.
- **Confirm step:** the page asks for confirmation before anything is injected.
- **While it runs:** the page follows the new experiment, so the charts show the effect live. The pipeline output
  is under "Pipeline output".
- **Emergency stop:** ends the pipeline's processes, then deletes every Chaos Mesh resource labelled with the
  experiment id.
- **Before a run:** the console checks that Prometheus and Jaeger answer at `PROMETHEUS_URL` / `JAEGER_URL` and
  that no quarantine is active.
- **Logs:** each run's output is kept in `<artifacts>/_runs/<run_id>.log`.

| Endpoint | |
|---|---|
| `GET /` | the page |
| `GET /api/v1/experiments` | ids with status, verdict, priority, aborted, quarantined; newest first |
| `GET /api/v1/experiments/{id}` | every JSON artifact present for that experiment, keyed by file name; `Quarantine` if it belongs to it |
| `POST /api/v1/experiments/{id}/ack` | deletes `QUARANTINE.json`. 404 if there is none, 409 if it belongs to another experiment |
| `GET /api/v1/inject` | faults with their parameters, targets with replica counts and eligibility, anything blocking a run |
| `POST /api/v1/runs` | `{target, fault, delay_ms, jitter_ms, loss_percent, duration, max_p99_ms}`: starts a run (202). 400 bad request, 409 a run is going or a quarantine is active |
| `GET /api/v1/runs/current` | the current or last run: state, experiment id, request, last 60 log lines |
| `POST /api/v1/runs/current/stop` | emergency stop |

POSTs must be `Content-Type: application/json`, and an `Origin` header, when sent, must be the console's own. So
another website open in the same browser cannot start or stop an injection.

The page polls every 2 s: status banner, three health charts from `HealthSeries.json` (checkout success rate,
edge p99, error rate; each with the baseline, the abort threshold / SLO line, the injection window and the abort
point), Red Agent panel, Blue Agent panel (root cause, cascade path, recommendation, resilience audit, diff),
and the buttons Download JSON Dossier, Copy Suggested Diff, Acknowledge Issue. Missing artifacts show
"waiting". The charts have a table view under "Table view".

## Tests

```bash
cd chaos && pytest tests/phase4
```

## Not done / known gaps

- Tested against the fixtures and against this repo's real source only. Not yet run on a live cluster or
  against a real Phase 3 RCA.
- Patches are generated for Go and Python call sites. C#, Java and Node call sites are localized by name only
  and get guidance without a code diff (the Istio part still applies).
- The deadline is derived from the edge p99 in the plan, not from the dependency's own latency (that lives in
  Phase 1's `topology.json`, which is not a contract). For a call that fans out further, such as frontend →
  checkoutservice `PlaceOrder`, 150 ms can be too tight; review the number before applying.
- No fallback-path template: a missing fallback is reported in the guidance only.
- The LLM path is untested against the real API (no key here); it is exercised only through its fallback.
- Stretch (re-run on a patched build, before/after view) is not built.
