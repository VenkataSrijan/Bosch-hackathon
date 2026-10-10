"""Operations console backend: a stateless HTTP server that reads ONLY the artifacts folder.

  GET  /                                  the console page
  GET  /api/v1/experiments                experiment ids with status, newest first
  GET  /api/v1/experiments/{id}           every artifact that exists for that experiment, as one JSON object
  POST /api/v1/experiments/{id}/ack       deletes ../QUARANTINE.json (human acknowledgement lifts the quarantine)

It never talks to Kubernetes or Chaos Mesh.
"""
from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from common.io import (
    ABORT_SIGNAL, EXPERIMENT_PLAN, INJECTION_RECEIPT, QUARANTINE, RECOMMENDATION, ROOT_CAUSE_ANALYSIS,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _load(path: Path) -> Optional[Any]:
    """Read a JSON file, or None if it is missing or momentarily unreadable (mid-rename on Windows)."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


class Console:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    # ------------------------------------------------------------------ helpers
    def _exp_dir(self, experiment_id: str) -> Optional[Path]:
        if not EXPERIMENT_ID.match(experiment_id):
            return None
        exp = (self.root / experiment_id).resolve()
        if exp.parent != self.root or not (exp / EXPERIMENT_PLAN).exists():
            return None
        return exp

    def _quarantine(self) -> Optional[dict]:
        q = _load(self.root / QUARANTINE)
        return q if isinstance(q, dict) else None

    # ------------------------------------------------------------------ API
    def list_experiments(self) -> list[dict]:
        quarantine = self._quarantine()
        rows = []
        if not self.root.is_dir():
            return rows
        for exp in self.root.iterdir():
            if not exp.is_dir() or not EXPERIMENT_ID.match(exp.name):
                continue
            plan = _load(exp / EXPERIMENT_PLAN)
            if not isinstance(plan, dict):
                continue
            receipt = _load(exp / INJECTION_RECEIPT) or {}
            rca = _load(exp / ROOT_CAUSE_ANALYSIS) or {}
            rec = _load(exp / RECOMMENDATION) or {}
            rows.append({
                "experiment_id": exp.name,
                "created_at": plan.get("created_at"),
                "target": (plan.get("target_node") or {}).get("service_name"),
                "status": receipt.get("status", "PLANNED"),
                "verdict": rca.get("verdict"),
                "priority": rec.get("priority"),
                "aborted": (exp / ABORT_SIGNAL).exists(),
                "quarantined": bool(quarantine and quarantine.get("experiment_id") == exp.name),
            })
        rows.sort(key=lambda r: (r["created_at"] or "", r["experiment_id"]), reverse=True)
        return rows

    def get_experiment(self, experiment_id: str) -> Optional[dict]:
        exp = self._exp_dir(experiment_id)
        if exp is None:
            return None
        body: dict[str, Any] = {"experiment_id": experiment_id}
        for path in sorted(exp.glob("*.json")):
            data = _load(path)
            if data is not None:
                body[path.stem] = data
        quarantine = self._quarantine()
        if quarantine and quarantine.get("experiment_id") == experiment_id:
            body["Quarantine"] = quarantine
        return body

    def ack(self, experiment_id: str) -> tuple[int, dict]:
        if self._exp_dir(experiment_id) is None:
            return HTTPStatus.NOT_FOUND, {"error": "unknown experiment"}
        quarantine = self._quarantine()
        path = self.root / QUARANTINE
        if quarantine is None:
            if path.exists():  # unreadable file: still a quarantine, let the human clear it
                path.unlink()
                return HTTPStatus.OK, {"acknowledged": True, "experiment_id": experiment_id}
            return HTTPStatus.NOT_FOUND, {"acknowledged": False, "error": "no active quarantine"}
        if quarantine.get("experiment_id") != experiment_id:
            return HTTPStatus.CONFLICT, {"acknowledged": False,
                                         "error": f"quarantine belongs to {quarantine.get('experiment_id')}"}
        path.unlink(missing_ok=True)
        return HTTPStatus.OK, {"acknowledged": True, "experiment_id": experiment_id}


def make_handler(console: Console) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ChaosConsole/1.0"

        def log_message(self, fmt: str, *args) -> None:  # keep the 2 s polling out of the terminal
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: Any) -> None:
            self._send(code, json.dumps(data, indent=2).encode("utf-8"), "application/json")

        def _parts(self) -> list[str]:
            return [p for p in self.path.split("?")[0].split("/") if p]

        def do_GET(self) -> None:
            parts = self._parts()
            if parts in ([], ["index.html"]):
                return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "v1", "experiments"]:
                return self._json(200, console.list_experiments())
            if len(parts) == 4 and parts[:3] == ["api", "v1", "experiments"]:
                body = console.get_experiment(parts[3])
                return self._json(200, body) if body else self._json(404, {"error": "unknown experiment"})
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            parts = self._parts()
            if len(parts) == 5 and parts[:3] == ["api", "v1", "experiments"] and parts[4] == "ack":
                code, body = console.ack(parts[3])
                return self._json(code, body)
            self._json(404, {"error": "not found"})

    return Handler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(Console(root)))
