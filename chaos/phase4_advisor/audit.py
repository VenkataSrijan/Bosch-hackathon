"""Resilience gap audit of a call site: deadline, retry with backoff, circuit breaker, fallback.

Each control is checked in the code (the calling function, then the service's own sources) and in the
service mesh configuration checked into this repo (Istio VirtualService / DestinationRule).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import yaml

from .localize import CallSite, read_lines, source_files

MESH_GLOBS = ("istio-manifests/**/*.yaml", "kustomize/components/**/*.yaml", "release/istio-manifests.yaml")

# Per control: a pattern for the calling function (by language, "*" = any) and, for controls that are
# usually configured once per client, a stricter pattern searched across the whole service.
FUNCTION_PATTERNS = {
    "timeout": {
        "go": r"context\.With(Timeout|Deadline)\(",
        "python": r"\btimeout\s*=",
        "node": r"\bdeadline\b|\btimeout\b",
        "csharp": r"\bdeadline\s*:|CancelAfter\(|Timeout\s*=",
        "java": r"withDeadline(After)?\(",
    },
    "retry": {"*": r"(?i)\bretr(y|ies)\b|backoff"},
    "circuit_breaker": {"*": r"(?i)breaker"},
    "fallback": {"*": r"(?i)fallback|degrad|cached?\b|stale"},
}
SERVICE_PATTERNS = {
    "retry": r"grpc_retry|retryPolicy|retry\.UnaryClientInterceptor|backoff\.Retry|retry\.Do\(|tenacity|@retry\b",
    "circuit_breaker": r"(?i)gobreaker|hystrix|circuit.?breaker|resilience4j|pybreaker|opossum|polly",
}

LABELS = {
    "timeout": "client-side deadline",
    "retry": "retry with backoff",
    "circuit_breaker": "circuit breaker",
    "fallback": "fallback path",
}


@dataclass
class Control:
    name: str
    present: bool
    evidence: str


def _grep(lines: list[str], start: int, end: int, pattern: str, rel: str) -> Optional[str]:
    rx = re.compile(pattern)
    for i in range(start, min(end, len(lines) - 1) + 1):
        if rx.search(lines[i]) and not lines[i].lstrip().startswith(("//", "#")):
            return f"{rel}:{i + 1}: {lines[i].strip()[:120]}"
    return None


def _mesh_docs(repo: Path):
    seen = set()
    for pattern in MESH_GLOBS:
        for path in sorted(repo.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            try:
                docs = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
            except (yaml.YAMLError, UnicodeDecodeError):
                continue
            for doc in docs:
                if isinstance(doc, dict):
                    yield path.relative_to(repo).as_posix(), doc


def _targets_host(doc: dict, host: str) -> bool:
    spec = doc.get("spec") or {}
    hosts = spec.get("hosts") or ([spec["host"]] if spec.get("host") else [])
    return any(str(h).split(".")[0] == host for h in hosts)


def mesh_controls(repo: Path, downstream: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for rel, doc in _mesh_docs(repo):
        if not _targets_host(doc, downstream):
            continue
        name = (doc.get("metadata") or {}).get("name", "?")
        if doc.get("kind") == "VirtualService":
            for route in (doc.get("spec") or {}).get("http") or []:
                if route.get("timeout"):
                    found.setdefault("timeout", f"{rel}: VirtualService {name} timeout {route['timeout']}")
                if route.get("retries"):
                    found.setdefault("retry", f"{rel}: VirtualService {name} retries")
        elif doc.get("kind") == "DestinationRule":
            policy = (doc.get("spec") or {}).get("trafficPolicy") or {}
            if policy.get("outlierDetection"):
                found.setdefault("circuit_breaker", f"{rel}: DestinationRule {name} outlierDetection")
    return found


def audit_call_site(repo: Path, site: CallSite, service_dir: Path, downstream: str) -> list[Control]:
    lines = read_lines(repo / site.file)
    service_files = [(p.relative_to(repo).as_posix(), read_lines(p)) for p in source_files(service_dir)]
    mesh = mesh_controls(repo, downstream)
    controls = []
    for name in ("timeout", "retry", "circuit_breaker", "fallback"):
        patterns = FUNCTION_PATTERNS[name]
        pattern = patterns.get(site.language) or patterns.get("*")
        evidence = _grep(lines, site.func_start, site.func_end, pattern, site.file) if pattern else None
        if evidence is None and name in SERVICE_PATTERNS:
            for rel, flines in service_files:
                evidence = _grep(flines, 0, len(flines) - 1, SERVICE_PATTERNS[name], rel)
                if evidence:
                    break
        if evidence is None and name in mesh:
            evidence = mesh[name]
        if evidence:
            controls.append(Control(name, True, evidence))
        else:
            where = f"{site.symbol} in {site.file}"
            if name in SERVICE_PATTERNS:
                where += f" or client setup in {service_dir.relative_to(repo).as_posix()}/"
            mesh_note = " or in mesh config" if name in ("timeout", "retry", "circuit_breaker") else ""
            controls.append(Control(name, False, f"not found in {where}{mesh_note}"))
    return controls


def as_dicts(controls: list[Control]) -> list[dict]:
    return [asdict(c) for c in controls]
