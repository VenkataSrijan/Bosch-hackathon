"""Atomic, validated reads and writes of artifacts.

Every artifact is validated against its model before it is written, and written to a
temp file in the same folder first, then renamed, so readers never see a half-written file.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, TypeVar

from pydantic import BaseModel

from .contracts import ARTIFACT_MODELS, HealthSample, HealthSeries, Quarantine

M = TypeVar("M", bound=BaseModel)

# File names inside chaos/artifacts/<experiment_id>/
EXPERIMENT_PLAN = "ExperimentPlan.json"
INJECTION_RECEIPT = "InjectionReceipt.json"
CANARY_VERDICT = "CanaryVerdict.json"
ABORT_SIGNAL = "AbortSignal.json"
HEALTH_SERIES = "HealthSeries.json"
ROOT_CAUSE_ANALYSIS = "RootCauseAnalysis.json"
RECOMMENDATION = "Recommendation.json"
QUARANTINE = "QUARANTINE.json"  # lives one level up, in the artifacts root


def quarantine_path(exp_dir: Path | str) -> Path:
    return Path(exp_dir).resolve().parent / QUARANTINE


def write_json_atomic(path: Path | str, data: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def _replace(src: str, dst: Path, attempts: int = 20) -> None:
    # On Windows os.replace fails while another process has the target open; retry briefly.
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)


def dump_model(model: BaseModel) -> Any:
    return model.model_dump(mode="json", by_alias=True)


def write_model(path: Path | str, model: BaseModel) -> Path:
    """Re-validate `model` and write it atomically."""
    validated = type(model).model_validate(dump_model(model))
    return write_json_atomic(path, dump_model(validated))


def write_artifact(exp_dir: Path | str, model: BaseModel) -> Path:
    """Write `model` to its canonical file name in `exp_dir`."""
    for name, cls in ARTIFACT_MODELS.items():
        if type(model) is cls:
            return write_model(Path(exp_dir) / name, model)
    raise TypeError(f"{type(model).__name__} is not an artifact model")


def read_json(path: Path | str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_model(path: Path | str, model_cls: type[M]) -> M:
    return model_cls.model_validate(read_json(path))


def read_artifact(exp_dir: Path | str, name: str) -> Optional[BaseModel]:
    """Read and validate `name` from `exp_dir`, or return None if it does not exist yet."""
    path = Path(exp_dir) / name
    if not path.exists():
        return None
    return read_model(path, ARTIFACT_MODELS[name])


def wait_for_file(path: Path | str, timeout_s: float, poll_s: float = 0.5) -> bool:
    """Poll until `path` exists. Returns False on timeout. Never sleeps more than 1 s per wait."""
    path = Path(path)
    deadline = time.monotonic() + timeout_s
    poll_s = min(poll_s, 1.0)
    while True:
        if path.exists():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_s, remaining))


def append_health_sample(exp_dir: Path | str, sample: HealthSample) -> Path:
    path = Path(exp_dir) / HEALTH_SERIES
    samples = read_model(path, HealthSeries).root if path.exists() else []
    samples.append(sample)
    return write_model(path, HealthSeries(samples))


def write_quarantine(exp_dir: Path | str, quarantine: Quarantine) -> Path:
    return write_model(quarantine_path(exp_dir), quarantine)
