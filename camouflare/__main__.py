from __future__ import annotations

import argparse

import uvicorn

from camouflare import __version__
from camouflare.app import create_app
from camouflare.config import Settings
from camouflare.observability import configure_logging

# Uvicorn otherwise waits indefinitely for in-flight requests (up to MAX_TIMEOUT_MS)
# before running lifespan shutdown. Bounding the drain keeps it plus the default
# SHUTDOWN_TIMEOUT_SECONDS cleanup inside the Compose 45-second stop grace period.
_GRACEFUL_REQUEST_DRAIN_SECONDS = 10


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="camouflare",
        description="Run the Camouflare FlareSolverr-compatible local service.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args()


def main() -> None:
    _parse_args()
    settings = Settings()
    configure_logging(level=settings.log_level_value, log_format=settings.log_format)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        timeout_graceful_shutdown=_GRACEFUL_REQUEST_DRAIN_SECONDS,
    )


if __name__ == "__main__":
    main()
