#!/usr/bin/env python3
"""Fetch Camoufox with optional pre-authenticated GitHub release metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

CAMOUFOX_RELEASES_API = "https://api.github.com/repos/daijro/camoufox/releases"
CAMOUFOX_RELEASE_TAGS_API = f"{CAMOUFOX_RELEASES_API}/tags"
RELEASE_METADATA_FILES = {
    CAMOUFOX_RELEASES_API: "CAMOUFLARE_CAMOUFOX_RELEASES_FILE",
}
DEFAULT_ARTIFACT_DIGESTS_FILE = Path(__file__).with_name("camoufox-artifacts.json")


class _ReleaseMetadataResponse:
    def __init__(self, payload: list[dict[str, Any]]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, Any]]:
        return self._payload


def _load_release_metadata(
    path: Path,
    *,
    expected_tag: str | None = None,
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    releases = [payload] if isinstance(payload, dict) else payload
    if not isinstance(releases, list) or not all(isinstance(item, dict) for item in releases):
        raise ValueError("Camoufox release metadata must be a JSON object or array of objects")
    if expected_tag is None:
        return releases

    matching = [release for release in releases if release.get("tag_name") == expected_tag]
    if len(matching) != 1:
        raise ValueError(
            f"Camoufox release metadata must contain exactly one {expected_tag!r} release"
        )
    return matching


def _load_artifact_manifest(path: Path) -> tuple[str, dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Camoufox artifact digest file must use schema version 1")
    release_tag = payload.get("release_tag")
    if (
        not isinstance(release_tag, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_tag) is None
    ):
        raise ValueError("Camoufox artifact digest file must contain a valid release_tag")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Camoufox artifact digest file must contain pinned artifacts")

    digests: dict[str, str] = {}
    for name, digest in artifacts.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".zip")
            or not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise ValueError(f"Invalid Camoufox artifact digest entry: {name!r}")
        digests[name] = digest.removeprefix("sha256:")
    return release_tag, digests


def _load_artifact_digests(path: Path) -> dict[str, str]:
    return _load_artifact_manifest(path)[1]


def _metadata_aware_get(
    original_get: Callable[..., Any],
    release_metadata: dict[str, list[dict[str, Any]]],
    *,
    release_tag: str | None = None,
) -> Callable[..., Any]:
    def get(url: str, *args: Any, **kwargs: Any) -> Any:
        normalized_url = url.rstrip("/")
        if normalized_url in release_metadata:
            return _ReleaseMetadataResponse(release_metadata[normalized_url])
        if normalized_url == CAMOUFOX_RELEASES_API and release_tag is not None:
            exact_url = f"{CAMOUFOX_RELEASE_TAGS_API}/{quote(release_tag, safe='')}"
            response = original_get(exact_url, *args, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("tag_name") != release_tag:
                raise ValueError(
                    f"Camoufox release endpoint did not return the pinned {release_tag!r} release"
                )
            return _ReleaseMetadataResponse([payload])
        return original_get(url, *args, **kwargs)

    return get


def _pinned_asset_checker(
    original_check_asset: Callable[..., Any],
    artifact_digests: dict[str, str],
) -> Callable[..., Any]:
    def check_asset(fetcher: Any, asset: dict[str, Any]) -> Any:
        if asset.get("name") not in artifact_digests:
            return None
        return original_check_asset(fetcher, asset)

    return check_asset


def _digest_verifying_download(
    original_download: Callable[..., Any],
    artifact_digests: dict[str, str],
) -> Callable[..., Any]:
    def download(file: Any, url: str) -> Any:
        artifact_name = Path(urlsplit(url).path).name
        expected_digest = artifact_digests.get(artifact_name)
        if expected_digest is None:
            raise RuntimeError(f"Camoufox artifact is not pinned: {artifact_name}")

        buffer = original_download(file, url)
        buffer.seek(0)
        actual = hashlib.sha256()
        while chunk := buffer.read(1024 * 1024):
            actual.update(chunk)
        buffer.seek(0)
        actual_digest = actual.hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise RuntimeError(
                f"Camoufox artifact digest mismatch for {artifact_name}: "
                f"expected sha256:{expected_digest}, got sha256:{actual_digest}"
            )
        return buffer

    return download


def main() -> int:
    release_tag, artifact_digests = _load_artifact_manifest(DEFAULT_ARTIFACT_DIGESTS_FILE)
    release_metadata = {
        api_url: _load_release_metadata(Path(metadata_path), expected_tag=release_tag)
        for api_url, env_name in RELEASE_METADATA_FILES.items()
        if (metadata_path := os.getenv(env_name)) and Path(metadata_path).is_file()
    }

    from camoufox import pkgman
    from camoufox.__main__ import cli

    original_get = pkgman.requests.get
    original_check_asset = pkgman.CamoufoxFetcher.check_asset
    original_download = pkgman.CamoufoxFetcher.download_file
    pkgman.requests.get = _metadata_aware_get(
        original_get,
        release_metadata,
        release_tag=release_tag,
    )
    pkgman.CamoufoxFetcher.check_asset = _pinned_asset_checker(
        original_check_asset,
        artifact_digests,
    )
    pkgman.CamoufoxFetcher.download_file = staticmethod(
        _digest_verifying_download(original_download, artifact_digests)
    )

    try:
        cli.main(args=["fetch"], prog_name="camoufox", standalone_mode=False)
    finally:
        pkgman.requests.get = original_get
        pkgman.CamoufoxFetcher.check_asset = original_check_asset
        pkgman.CamoufoxFetcher.download_file = staticmethod(original_download)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
