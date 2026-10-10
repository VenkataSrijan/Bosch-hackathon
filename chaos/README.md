# Chaos platform

Autonomous, closed-loop chaos engineering for Online Boutique. A Red Agent (Phases 1–2) plans and injects
faults; a Blue Agent (Phases 3–4) watches, aborts, explains and recommends a fix. Phases talk only through
JSON files in `artifacts/<experiment_id>/`.

| Phase | Folder | Owner | Writes |
|---|---|---|---|
| 1. Observation and Dynamic Profiling (Red) | `phase1_observation/` | Pardhiv | `ExperimentPlan.json` |
| 2. Autonomous Injection Engine (Red) | `phase2_injection/` | Shankar Aditya | `InjectionReceipt.json` |
| 3. Diagnostic Judge and Abort Switch (Blue) | `phase3_judge/` | Aarya | `CanaryVerdict.json`, `AbortSignal.json`, `HealthSeries.json`, `RootCauseAnalysis.json`, `../QUARANTINE.json` |
| 4. Remediation Advisor + Console (Blue) | `phase4_advisor/`, `console/` | Srijan | `Recommendation.json` |

## Quick start (no cluster)

```bash
cd chaos
pip install -r requirements.txt
python run_pipeline.py --mock     # artifacts land in artifacts/mock/<experiment_id>/
pytest
```

Until a phase is merged, its folder holds a stub that copies the matching fixture.

## Live

```bash
bash chaos/scripts/setup_cluster.sh            # kind + Istio + Prometheus + Jaeger + Online Boutique + Chaos Mesh
kubectl -n istio-system port-forward svc/prometheus 9090:9090 &
kubectl -n istio-system port-forward svc/tracing 16686:80 &
cd chaos && python run_pipeline.py
python -m console --artifacts artifacts --port 8080
```

## Shared, frozen files

`common/`, `contracts/`, `fixtures/`, `scripts/setup_cluster.sh`, `run_pipeline.py`, `requirements.txt`,
`pytest.ini`, `tests/test_contracts.py`. They change only through a commit by Srijan on `main` after the team
agrees. Phase branches never edit them.

- `common/contracts.py`: one pydantic model per artifact, plus the enums. The models are the contract.
- `common/io.py`: `write_artifact(exp_dir, model)` validates and writes atomically; `read_artifact(exp_dir, name)`
  returns `None` when the file is not there yet; `wait_for_file()`, `append_health_sample()`, `write_quarantine()`.
- `common/config.py`: `get_settings()` reads the environment variables below.
- `common/llm.py`: `complete(system, user) -> str`. Raises `LLMUnavailable` when no provider is configured or
  the call fails; callers must fall back to rule-based logic.
- `contracts/*.schema.json`: generated. After a model change run `python -m common.schemas`.

## Configuration

| Variable | Default |
|---|---|
| `KUBE_NAMESPACE` | `default` |
| `PROMETHEUS_URL` | `http://localhost:9090` |
| `JAEGER_URL` | `http://localhost:16686` |
| `ARTIFACTS_DIR` | `artifacts` (relative to `chaos/`) |
| `SLO_CHECKOUT_SUCCESS_PERCENT` | `99.5` |
| `SAFETY_ALPHA` | `0.10` |
| `CANARY_SECONDS` | `10` |
| `CANARY_VERDICT_TIMEOUT_SECONDS` | `30` |
| `LLM_PROVIDER` | empty (no LLM). `anthropic` is supported |
| `LLM_API_KEY` | empty |
| `LLM_MODEL` | `claude-opus-5-5` |

## Phase CLIs (run from `chaos/`)

```
python -m phase1_observation --out <ARTIFACTS_DIR> [--mock]      # last stdout line = experiment folder
python -m phase2_injection  --exp <experiment folder> [--mock]
python -m phase3_judge      --exp <experiment folder> [--mock]
python -m phase4_advisor    --exp <experiment folder> --repo <repo root> [--mock]
python -m console           --artifacts <ARTIFACTS_DIR> [--port 8080]
```

Exit 0 on success, including a clean abort or a NO_GO.

## Mock-mode notes

- `run_pipeline.py --mock` writes under `<ARTIFACTS_DIR>/mock/` so mock runs never mix with live ones, and it
  clears a stale `mock/QUARANTINE.json` before starting (a mock abort injected nothing real).
- A live run refuses to start while `<ARTIFACTS_DIR>/QUARANTINE.json` exists. Acknowledge it in the console.

## Tests

Shared tests live in `tests/test_contracts.py`; each phase adds `tests/phase<N>/`. `pytest.ini` uses
`--import-mode=importlib`, so test files in different phase folders may share names.
