# Phase 2 — Autonomous Injection Engine (Red Agent)

Turns `ExperimentPlan.json` into a real Chaos Mesh fault, safely, and records what
it did in `InjectionReceipt.json`. It is the Red Agent's hands: **no telemetry
reads, no source-code reads, no judgement of results.**

```
python -m phase2_injection --exp <experiment folder> [--mock]
```

Exit code `0` on success — including a clean abort or a `NO_GO`. Non-zero only on
a real failure (bad/missing plan, active quarantine, pre-flight failure).

## What it does

1. **Pre-flight** — refuses to run if `../QUARANTINE.json` exists; confirms the
   fault maps to a Chaos Mesh resource; confirms the target Deployment has
   `replicas_total` ready pods and at least one running pod.
2. **Manifest synthesis** — builds a Chaos Mesh resource from the plan and writes
   it to `<experiment_id>-net.yaml` (`-pod.yaml` / `-stress.yaml` for other
   kinds) in the experiment folder. Labelled `chaos.ai/experiment-id=<id>` so the
   Blue Agent can delete it on abort.
3. **Canary ramp** — applies the fault for `CANARY_SECONDS`, publishes the receipt
   as `RUNNING`, removes the fault, then waits up to `CANARY_VERDICT_TIMEOUT_SECONDS`
   for `CanaryVerdict.json`. No verdict or `NO_GO` → final status `NO_GO`, stop.
   `GO` → re-apply for the rest of the window.
4. **Dead-man's switch** — every applied resource carries a Chaos Mesh `duration`
   no longer than its window, so the fault self-clears if this process dies. The
   engine also deletes its resources by label on normal exit, on an abort, on
   SIGINT / SIGTERM, and on any unhandled exception.
5. **Abort awareness** — polls for `AbortSignal.json`; the moment it appears the
   engine stops and sets the final status to `ABORTED`.
6. **Receipt** — `InjectionReceipt.json` with the real target pod, the fault
   actually applied, `initiated_at`, `hard_timeout_at = initiated_at +
   max_duration_seconds`, and `ended_at` with the final status
   (`RUNNING → COMPLETED | ABORTED | NO_GO | FAILED`).

## Fault types

| plan `category` / `action` | Chaos Mesh resource | notes |
|---|---|---|
| `network` / `latency` (or `delay`) | `NetworkChaos` delay | **demo path**; `delay_ms`, `jitter_ms` |
| `network` / `loss` | `NetworkChaos` loss | `loss_percent` |
| `pod` / `pod-kill` | `PodChaos` pod-kill | one-shot, no duration |
| `stress` / `cpu` | `StressChaos` cpu | `workers`, `load` |
| `stress` / `memory` | `StressChaos` memory | `workers`, `size` |

`fault_profile.parameters.target_pods` chooses how many pods to hit (fixed mode),
clamped to `targetable_replicas` so the last replica is never touched. An optional
`parameters.upstream_caller` scopes a NetworkChaos to one caller
(`direction: to`, `target: <caller>`); without it the latency is applied to all of
the target pod's traffic.

## Live run

Needs a cluster with Chaos Mesh and the target app (see
`chaos/scripts/setup_cluster.sh`), and `kubectl` on PATH pointing at it.

```bash
kubectl apply -f chaos/deploy/rbac/red-injector.yaml   # namespace-scoped RBAC
cd chaos
python -m phase2_injection --exp artifacts/<experiment_id>
```

Usually driven by `python run_pipeline.py` alongside Phases 1, 3 and 4.

## Mock run

```bash
cd chaos
python -m phase2_injection --exp artifacts/mock/<experiment_id> --mock
```

No cluster calls; the same handshake runs with waits capped at ~1 second, against
a recorded `ExperimentPlan.json` and the Blue Agent's artifacts (the stubs or
Phase 3 supply `CanaryVerdict.json` / `AbortSignal.json`).

## Dependencies

`PyYAML` (optional — the manifest falls back to JSON, which is valid YAML) and the
shared `common/` package. `kubectl` must be on PATH for live runs.

## Tested

- `tests/phase2/test_manifest.py` — manifest synthesis for every fault type,
  caller scoping, and the unsupported-fault error.
- `tests/phase2/test_engine.py` — the full handshake in mock mode: `GO→COMPLETED`,
  `GO→ABORTED`, `NO_GO`, verdict timeout, quarantine refusal, unsupported fault,
  and a failed replica pre-flight.
- `python run_pipeline.py --mock` exits 0 with this engine in place of the stub.

## Known gaps / not done

- **Mock only so far.** Not yet exercised against a live Chaos Mesh cluster (none
  was available while building this). The kubectl calls and the NetworkChaos
  schema follow Chaos Mesh 2.7.x; verify on the cluster before the demo.
- `cgroup_id` reports the pod UID (`pod:<uid>`), not a true cgroup slice — Chaos
  Mesh scopes by pod and the exact cgroup id isn't surfaced to the client.
- Phase 3's documented abort deletes `networkchaos` by label only; the `pod` and
  `stress` fault kinds rely on this engine's own cleanup / dead-man's switch for
  teardown.
- The upstream-caller scoping is opt-in via `parameters.upstream_caller` because
  the frozen `ExperimentPlan` contract carries no caller field (see the note to
  the team).
