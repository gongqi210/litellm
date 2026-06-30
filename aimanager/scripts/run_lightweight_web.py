from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from aimanager.lightweight_web import create_app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AiManager lightweight non-SDK web entry.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument("--business-base-url", default=None)
    args = parser.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("BLOCKED lightweight web: uvicorn is required to run the ASGI app\n")
        return 2

    uvicorn.run(create_app(business_base_url=args.business_base_url), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
