"""Operations console: one page that shows a whole experiment, read from the artifacts folder only."""
from __future__ import annotations

import argparse
from pathlib import Path

from common.config import get_settings

from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="console", description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=None, help="artifacts root (default: ARTIFACTS_DIR)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    args = parser.parse_args(argv)

    root = (args.artifacts or get_settings().artifacts_dir).resolve()
    httpd = serve(root, args.host, args.port)
    print(f"[console] reading {root}")
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
