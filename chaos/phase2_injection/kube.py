"""Cluster operations for Phase 2: a kubectl-backed injector and a no-op mock.

Phase 2 is the Red Agent's hands. It may only: look up the target pods by label,
apply a Chaos Mesh resource, and delete Chaos Mesh resources by label. It never
reads telemetry, logs or source code and never judges a result.

``ClusterInjector`` shells out to ``kubectl``. ``MockInjector`` performs no
cluster calls and is used by ``--mock`` and the unit tests.
"""
from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from .manifest import LABEL_KEY

# Chaos Mesh kinds Phase 2 may create and must therefore be able to delete.
_CHAOS_KINDS = ("networkchaos", "podchaos", "stresschaos")


class InjectorError(RuntimeError):
    pass


class Injector(ABC):
    """Everything Phase 2 is allowed to do against the cluster."""

    mock: bool = False

    @abstractmethod
    def ready_replicas(self, namespace: str, deployment: str) -> int:
        ...

    @abstractmethod
    def pods_for(self, namespace: str, service_name: str) -> list[str]:
        ...

    @abstractmethod
    def pod_uid(self, namespace: str, pod: str) -> Optional[str]:
        ...

    @abstractmethod
    def apply(self, manifest_path: Path) -> None:
        ...

    @abstractmethod
    def delete_by_label(self, namespace: str, experiment_id: str) -> None:
        ...


def _kubectl(args: list[str], *, check: bool = True, timeout: float = 30.0) -> str:
    exe = shutil.which("kubectl")
    if not exe:
        raise InjectorError("kubectl was not found on PATH")
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise InjectorError(f"kubectl {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise InjectorError(f"kubectl {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


class ClusterInjector(Injector):
    mock = False

    def ready_replicas(self, namespace: str, deployment: str) -> int:
        out = _kubectl(["-n", namespace, "get", "deployment", deployment,
                        "-o", "jsonpath={.status.readyReplicas}"])
        return int(out) if out else 0

    def pods_for(self, namespace: str, service_name: str) -> list[str]:
        out = _kubectl(["-n", namespace, "get", "pods", "-l", f"app={service_name}",
                        "--field-selector=status.phase=Running",
                        "-o", "jsonpath={.items[*].metadata.name}"])
        return out.split() if out else []

    def pod_uid(self, namespace: str, pod: str) -> Optional[str]:
        try:
            return _kubectl(["-n", namespace, "get", "pod", pod, "-o", "jsonpath={.metadata.uid}"]) or None
        except InjectorError:
            return None

    def apply(self, manifest_path: Path) -> None:
        _kubectl(["apply", "-f", str(manifest_path)])

    def delete_by_label(self, namespace: str, experiment_id: str) -> None:
        for kind in _CHAOS_KINDS:
            # Best effort, idempotent: Phase 3's abort may already have deleted these.
            _kubectl(["delete", kind, "-n", namespace, "-l", f"{LABEL_KEY}={experiment_id}",
                      "--ignore-not-found", "--wait=false"], check=False)


class MockInjector(Injector):
    """No cluster. Records what it was asked to do so tests can assert on it."""

    mock = True

    def __init__(self, replicas_total: int = 2) -> None:
        self._replicas = replicas_total
        self.applied: list[Path] = []
        self.deleted: list[str] = []

    def ready_replicas(self, namespace: str, deployment: str) -> int:
        return self._replicas

    def pods_for(self, namespace: str, service_name: str) -> list[str]:
        return [f"{service_name}-mock-{i}" for i in range(self._replicas)]

    def pod_uid(self, namespace: str, pod: str) -> Optional[str]:
        return f"mock-uid-{pod}"

    def apply(self, manifest_path: Path) -> None:
        self.applied.append(Path(manifest_path))

    def delete_by_label(self, namespace: str, experiment_id: str) -> None:
        self.deleted.append(experiment_id)
