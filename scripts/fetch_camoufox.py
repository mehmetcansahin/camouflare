#!/usr/bin/env python3
"""Fetch Camoufox with optional pre-authenticated GitHub release metadata."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

CAMOUFOX_RELEASES_API = "https://api.github.com/repos/daijro/camoufox/releases"
GEOLITE_RELEASES_API = "https://api.github.com/repos/P3TERX/GeoLite.mmdb/releases"
RELEASE_METADATA_FILES = {
    CAMOUFOX_RELEASES_API: "CAMOUFLARE_CAMOUFOX_RELEASES_FILE",
    GEOLITE_RELEASES_API: "CAMOUFLARE_GEOLITE_RELEASES_FILE",
}


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
    release_metadata: dict[str, list[dict[str, Any]]],
) -> Callable[..., Any]:
    def get(url: str, *args: Any, **kwargs: Any) -> Any:
        normalized_url = url.rstrip("/")
        if normalized_url in release_metadata:
            return _ReleaseMetadataResponse(release_metadata[normalized_url])
        return original_get(url, *args, **kwargs)

    return get


def main() -> int:
    release_metadata = {
        api_url: _load_release_metadata(Path(metadata_path))
        for api_url, env_name in RELEASE_METADATA_FILES.items()
        if (metadata_path := os.getenv(env_name)) and Path(metadata_path).is_file()
    }

    from camoufox import pkgman
    from camoufox.__main__ import cli

    original_get = pkgman.requests.get
    if release_metadata:
        pkgman.requests.get = _metadata_aware_get(original_get, release_metadata)

    try:
        cli.main(args=["fetch"], prog_name="camoufox", standalone_mode=False)
    finally:
        pkgman.requests.get = original_get
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
