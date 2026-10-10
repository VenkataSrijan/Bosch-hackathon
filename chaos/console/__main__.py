"""Operations console: one page that shows a whole experiment, read from the artifacts folder only."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common.config import get_settings

from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="console", description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=None,
                        help="artifacts root (default: ARTIFACTS_DIR, or ARTIFACTS_DIR/mock with --mock)")
    parser.add_argument("--mock", action="store_true", help="injections from this console run with --mock (no cluster)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    args = parser.parse_args(argv)

    settings = get_settings()
    root = (args.artifacts or (settings.artifacts_dir / "mock" if args.mock else settings.artifacts_dir)).resolve()
    try:
        httpd = serve(root, args.host, args.port, mock=args.mock)
    except OSError as exc:
        print(f"[console] cannot listen on {args.host}:{args.port} ({exc.strerror or exc}); "
              f"another program may be using it, try --port 8090", file=sys.stderr)
        return 1
    print(f"[console] reading {root}; injections run {'with --mock' if args.mock else 'live against the cluster'}")
    print(f"[console] open http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
