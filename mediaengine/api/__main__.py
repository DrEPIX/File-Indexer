"""Run the HTTP service with ``python -m mediaengine.api``."""

from __future__ import annotations

import argparse

from ..config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MediaEngine HTTP API")
    parser.add_argument("--config")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.host:
        config.api.host = args.host
    if args.port:
        config.api.port = args.port

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("install mediaengine[api] to run the server") from exc
    from .app import create_app

    uvicorn.run(create_app(config), host=config.api.host, port=config.api.port)


if __name__ == "__main__":
    main()

