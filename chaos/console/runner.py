"""Fault injection from the console: list targets, check readiness, start and stop one pipeline run.

The console starts `run_pipeline.py` with the operator's choice; every safety rule stays in the phases
(two replicas, canary, abort limits, quarantine). One run at a time. Output goes to <root>/_runs/<run_id>.log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from common.config import CHAOS_ROOT, get_settings
from common.io import EXPERIMENT_PLAN, QUARANTINE

RUNS_DIR = "_runs"
CONSOLE_STOP = "ConsoleStop.json"  # the console's own record that an operator stopped this experiment
EXPERIMENT_ID = re.compile(r"exp-[a-z0-9]+-[a-z0-9-]+-\d{3}")
LABEL_KEY = "chaos.ai/experiment-id"
CHAOS_KINDS = "networkchaos,podchaos,stresschaos"

# What the form offers. Bounds match Phase 1's validation; Phase 1 checks again.
FAULTS: list[dict[str, Any]] = [
    {"id": "latency", "label": "Network latency", "params": [
        {"name": "delay_ms", "label": "Delay", "unit": "ms", "default": 250, "min": 1, "max": 5000},
        {"name": "jitter_ms", "label": "Jitter", "unit": "ms", "default": 20, "min": 0, "max": 5000}]},
    {"id": "loss", "label": "Packet loss", "params": [
        {"name": "loss_percent", "label": "Packets dropped", "unit": "%", "default": 30, "min": 1, "max": 100}]},
    {"id": "pod-kill", "label": "Pod kill", "params": []},
]
DURATION = {"default": 60, "min": 10, "max": 600}


class RunError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kubectl(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    exe = shutil.which("kubectl")
    if exe is None:
        return 127, "kubectl not found on PATH"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"kubectl {' '.join(args)} timed out"
    return p.returncode, (p.stdout if p.returncode == 0 else (p.stderr or p.stdout)).strip()


def _reachable(url: str) -> Optional[str]:
    """None when `url` answers, else why not."""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            resp.read(256)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


@dataclass
class Run:
    run_id: str
    request: dict[str, Any]
    log_path: Path
    process: subprocess.Popen
    started_at: str = field(default_factory=_utcnow)
    ended_at: Optional[str] = None
    stopped: bool = False
    stop_note: Optional[str] = None

    def experiment_id(self) -> Optional[str]:
        try:
            m = EXPERIMENT_ID.search(self.log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return None
        return m.group(0) if m else None

    def to_dict(self) -> dict[str, Any]:
        code = self.process.poll()
        if code is not None and self.ended_at is None:
            self.ended_at = _utcnow()
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        state = "running" if code is None else "stopped" if self.stopped else "finished" if code == 0 else "failed"
        return {"run_id": self.run_id, "state": state, "exit_code": code, "request": self.request,
                "experiment_id": self.experiment_id(), "started_at": self.started_at, "ended_at": self.ended_at,
                "stop_note": self.stop_note, "log_tail": lines[-60:]}


class RunManager:
    def __init__(self, root: Path, mock: bool, *,
                 kubectl: Callable[[list[str]], tuple[int, str]] = _kubectl,
                 reachable: Callable[[str], Optional[str]] = _reachable,
                 python: str = sys.executable):
        self.root = Path(root).resolve()
        self.mock = mock
        self.kubectl = kubectl
        self.reachable = reachable
        self.python = python
        self.lock = threading.Lock()
        self.run: Optional[Run] = None
        settings = get_settings()
        self.namespace = settings.kube_namespace
        self.prometheus_url = settings.prometheus_url
        self.jaeger_url = settings.jaeger_url

    # ---------------------------------------------------------------- what can be injected
    def targets(self) -> tuple[list[dict], Optional[str]]:
        if self.mock:
            from phase1_observation.recorded_data import RECORDED_DEPLOYMENTS
            deps = [(d["name"], int(d["replicas_total"])) for d in RECORDED_DEPLOYMENTS]
        else:
            code, out = self.kubectl(["-n", self.namespace, "get", "deployments", "-o", "json"])
            if code != 0:
                return [], f"cannot list deployments in {self.namespace!r}: {out}"
            deps = [(d["metadata"]["name"], int(d["spec"].get("replicas") or 0)) for d in json.loads(out)["items"]]
        rows = []
        for name, replicas in sorted(deps):
            if name in ("loadgenerator", "redis-cart"):
                continue
            eligible = replicas >= 2
            rows.append({"service": name, "replicas": replicas, "eligible": eligible,
                         "reason": None if eligible else
                         f"needs 2 replicas so one stays untouched (kubectl scale deployment {name} --replicas=2)"})
        return rows, None

    def readiness(self) -> list[str]:
        """Problems that would stop a run now; empty when ready."""
        problems = []
        if (self.root / QUARANTINE).exists():
            q = json.loads((self.root / QUARANTINE).read_text(encoding="utf-8"))
            problems.append(f"quarantined by {q.get('experiment_id')}: acknowledge that experiment first")
        if self.mock:
            return problems
        why = self.reachable(f"{self.prometheus_url.rstrip('/')}/-/ready")
        if why:
            problems.append(f"Prometheus not reachable at {self.prometheus_url} ({why}); run "
                            "kubectl -n istio-system port-forward svc/prometheus 9090:9090")
        why = self.reachable(f"{self.jaeger_url.rstrip('/')}/api/services")
        if why:
            problems.append(f"Jaeger not reachable at {self.jaeger_url} ({why}); run "
                            "kubectl -n istio-system port-forward svc/tracing 16686:80")
        return problems

    def options(self) -> dict[str, Any]:
        targets, target_error = self.targets()
        problems = self.readiness()
        if target_error:
            problems.append(target_error)
        current = self.current()
        return {"mode": "mock" if self.mock else "live", "namespace": self.namespace, "faults": FAULTS,
                "duration": DURATION, "targets": targets, "problems": problems,
                "running": bool(current and current["state"] == "running")}

    # ---------------------------------------------------------------- propose (agent decides, human approves)
    def propose(self, body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Run Phase 1 only (the Red Agent picks a target + fault) WITHOUT injecting.

        The operator reviews the proposal and then approves it through the normal
        POST /api/v1/runs path, so every safety rule still applies at injection time.
        Phase 1 writes into a throwaway `_proposals/` dir, never the experiments list.
        """
        with self.lock:
            if self.run and self.run.process.poll() is None:
                raise RunError(409, f"run {self.run.run_id} is still going; wait for it or stop it")
        out_dir = self.root / "_proposals"
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [self.python, "-u", "-m", "phase1_observation", "--out", str(out_dir)]
        if self.mock:
            cmd.append("--mock")
        else:
            cmd += ["--window", "2"]  # quick baseline just for the proposal; the approved run profiles again
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = subprocess.run(cmd, cwd=CHAOS_ROOT, capture_output=True, text=True, env=env, timeout=600)
        except subprocess.TimeoutExpired:
            raise RunError(504, "the agent took too long to build a proposal")
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-400:] or "unknown error"
            raise RunError(502, f"the agent could not build a proposal: {detail}")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        exp_dir = Path(lines[-1].strip()) if lines else None
        plan: Any = None
        if exp_dir and (exp_dir / EXPERIMENT_PLAN).exists():
            try:
                plan = json.loads((exp_dir / EXPERIMENT_PLAN).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                plan = None
        if not isinstance(plan, dict):
            raise RunError(502, "the agent produced no ExperimentPlan")

        tn = plan.get("target_node") or {}
        fp = plan.get("fault_profile") or {}
        at = plan.get("abort_thresholds") or {}
        params = fp.get("parameters") or {}
        request: dict[str, Any] = {"target": tn.get("service_name"), "fault": fp.get("action")}
        for key in ("delay_ms", "jitter_ms", "loss_percent"):
            if params.get(key) is not None:
                request[key] = params[key]
        if fp.get("max_duration_seconds") is not None:
            request["duration"] = fp["max_duration_seconds"]
        if at.get("max_p99_latency_ms") is not None:
            request["max_p99_ms"] = at["max_p99_latency_ms"]
        rationale = [ln for ln in lines if ln.startswith("[phase1]") and "Wrote" not in ln]
        return {
            "proposed_by": "red-agent",
            "experiment_id": plan.get("experiment_id"),
            "request": request,
            "steady_state_baseline": plan.get("steady_state_baseline"),
            "abort_thresholds": at,
            "rationale": rationale,
            "blocked_by": self.readiness(),  # e.g. a standing quarantine that would block the approved run
        }

    # ---------------------------------------------------------------- start / stop
    def _validate(self, body: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        if not isinstance(body, dict):
            raise RunError(400, "expected a JSON object")
        fault = next((f for f in FAULTS if f["id"] == body.get("fault")), None)
        if fault is None:
            raise RunError(400, f"fault must be one of {', '.join(f['id'] for f in FAULTS)}")
        targets, error = self.targets()
        if error:
            raise RunError(503, error)
        target = next((t for t in targets if t["service"] == body.get("target")), None)
        if target is None:
            raise RunError(400, f"unknown target {body.get('target')!r}")
        if not target["eligible"]:
            raise RunError(400, f"{target['service']} {target['reason']}")

        def number(name: str, lo: float, hi: float, integer: bool = True) -> Optional[float]:
            value = body.get(name)
            if value in (None, ""):
                return None
            try:
                value = int(value) if integer else float(value)
            except (TypeError, ValueError):
                raise RunError(400, f"{name} must be a number")
            if not lo <= value <= hi:
                raise RunError(400, f"{name} must be between {lo:g} and {hi:g}")
            return value

        args = ["--target", target["service"], "--fault", fault["id"]]
        request: dict[str, Any] = {"target": target["service"], "fault": fault["id"]}
        for p in fault["params"]:
            value = number(p["name"], p["min"], p["max"])
            if value is not None:
                args += [f"--{p['name'].replace('_', '-')}", str(value)]
                request[p["name"]] = value
        duration = number("duration", DURATION["min"], DURATION["max"])
        if duration is not None:
            args += ["--duration", str(duration)]
            request["duration"] = duration
        limit = number("max_p99_ms", 1, 60000, integer=False)
        if limit is not None:
            args += ["--max-p99-ms", f"{limit:g}"]
            request["max_p99_ms"] = limit
        return args, request

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.run and self.run.process.poll() is None:
                raise RunError(409, f"run {self.run.run_id} is still going; wait for it or stop it")
            problems = self.readiness()
            if problems:
                raise RunError(409, "; ".join(problems))
            args, request = self._validate(body)
            run_id = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ")
            log_dir = self.root / RUNS_DIR
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{run_id}.log"
            cmd = [self.python, "-u", str(CHAOS_ROOT / "run_pipeline.py"), "--artifacts", str(self.root), *args]
            if self.mock:
                cmd.append("--mock")
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            popen: dict[str, Any] = {}
            if os.name == "nt":
                popen["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen["start_new_session"] = True
            log = open(log_path, "w", encoding="utf-8")
            log.write(f"$ {' '.join(cmd[1:])}\n")
            log.flush()
            try:
                process = subprocess.Popen(cmd, cwd=CHAOS_ROOT, stdout=log, stderr=subprocess.STDOUT, env=env, **popen)
            finally:
                log.close()
            self.run = Run(run_id=run_id, request=request, log_path=log_path, process=process)
            return self.run.to_dict()

    def stop(self) -> dict[str, Any]:
        """Emergency stop: end the pipeline's processes, then delete the experiment's chaos resources."""
        with self.lock:
            run = self.run
            if run is None or run.process.poll() is not None:
                raise RunError(404, "no run is going")
            run.stopped = True
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(run.process.pid), "/T", "/F"], capture_output=True)
            else:
                try:
                    os.killpg(run.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                run.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                run.process.kill()
            eid = run.experiment_id()
            if self.mock or eid is None:
                run.stop_note = "stopped; no cluster resources to remove" if self.mock else "stopped before a plan existed"
            else:
                code, out = self.kubectl(["delete", CHAOS_KINDS, "-n", self.namespace, "-l", f"{LABEL_KEY}={eid}",
                                          "--now", "--ignore-not-found"])
                run.stop_note = f"stopped; chaos resources for {eid}: {out or 'none left'}" if code == 0 \
                    else f"stopped, but removing chaos resources FAILED: {out}"
            with open(run.log_path, "a", encoding="utf-8") as fh:
                fh.write(f"[console] {run.stop_note}\n")
            if eid and (self.root / eid).is_dir():
                (self.root / eid / CONSOLE_STOP).write_text(json.dumps(
                    {"experiment_id": eid, "stopped_at": _utcnow(), "note": run.stop_note}, indent=2), encoding="utf-8")
            return run.to_dict()

    def current(self) -> Optional[dict[str, Any]]:
        return self.run.to_dict() if self.run else None

    def wait(self, timeout: float = 600) -> Optional[int]:
        """For tests: wait for the current run to end."""
        deadline = time.monotonic() + timeout
        while self.run and self.run.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        return self.run.process.poll() if self.run else None
