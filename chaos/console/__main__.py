"""STUB for the Operations Console. Replace with the real implementation.

Serves the artifacts folder read-only as JSON:
  GET /api/v1/experiments          experiment ids
  GET /api/v1/experiments/{id}     every artifact that exists for that experiment
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from common.config import get_settings
from common.io import EXPERIMENT_PLAN, read_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="console", description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=None)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    root = (args.artifacts or get_settings().artifacts_dir).resolve()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["api", "v1", "experiments"]:
                ids = sorted(p.name for p in root.glob("*") if (p / EXPERIMENT_PLAN).exists())
                return self._send(200, [{"experiment_id": i} for i in ids])
            if len(parts) == 4 and parts[:3] == ["api", "v1", "experiments"]:
                exp = root / parts[3]
                if exp.parent != root or not exp.is_dir():
                    return self._send(404, {"error": "unknown experiment"})
                return self._send(200, {f.stem: read_json(f) for f in sorted(exp.glob("*.json"))})
            self._send(404, {"error": "not found"})

    print(f"[console stub] serving {root} on http://localhost:{args.port}")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
