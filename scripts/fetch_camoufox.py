#!/usr/bin/env python3
"""Fetch Camoufox with optional pre-authenticated GitHub release metadata."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

CAMOUFOX_RELEASES_API = "https://api.github.com/repos/daijro/camoufox/releases"
RELEASES_FILE_ENV = "CAMOUFLARE_CAMOUFOX_RELEASES_FILE"


class _ReleaseMetadataResponse:
    def __init__(self, payload: list[dict[str, Any]]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return self._payload


def _load_release_metadata(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Camoufox release metadata must be a JSON array of objects")
    return payload


def _metadata_aware_get(
    original_get: Callable[..., Any],
    release_metadata: list[dict[str, Any]],
) -> Callable[..., Any]:
    def get(url: str, *args: Any, **kwargs: Any) -> Any:
        if url.rstrip("/") == CAMOUFOX_RELEASES_API:
            return _ReleaseMetadataResponse(release_metadata)
        return original_get(url, *args, **kwargs)

    return get


def main() -> int:
    metadata_path = os.getenv(RELEASES_FILE_ENV)

    from camoufox import pkgman
    from camoufox.__main__ import cli

    original_get = pkgman.requests.get
    if metadata_path and Path(metadata_path).is_file():
        release_metadata = _load_release_metadata(Path(metadata_path))
        pkgman.requests.get = _metadata_aware_get(original_get, release_metadata)

    try:
        cli.main(args=["fetch"], prog_name="camoufox", standalone_mode=False)
    finally:
        pkgman.requests.get = original_get
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
