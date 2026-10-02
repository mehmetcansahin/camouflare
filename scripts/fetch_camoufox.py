#!/usr/bin/env python3
"""Install the Camoufox browser and default add-ons pinned in camoufox-artifacts.json.

Every download is selected from the manifest and verified by SHA-256 before it is
extracted; any failure is fatal. The upstream ``camoufox fetch`` command is not used
because it resolves the latest browser, add-on, and GeoIP releases and ignores add-on
download failures. The GeoIP database is intentionally not fetched: Camouflare never
enables Camoufox's ``geoip`` option.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

CAMOUFOX_RELEASES_API = "https://api.github.com/repos/daijro/camoufox/releases"
CAMOUFOX_RELEASE_TAGS_API = f"{CAMOUFOX_RELEASES_API}/tags"
RELEASE_METADATA_FILES = {
    CAMOUFOX_RELEASES_API: "CAMOUFLARE_CAMOUFOX_RELEASES_FILE",
}
DEFAULT_ARTIFACT_DIGESTS_FILE = Path(__file__).with_name("camoufox-artifacts.json")
_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class _AddonPin:
    version: str
    url: str
    sha256: str


@dataclass(frozen=True)
class _ArtifactManifest:
    release_tag: str
    artifacts: dict[str, str]
    addons: dict[str, _AddonPin]


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


def _load_addon_pin(name: object, pin: object) -> _AddonPin:
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) is None:
        raise ValueError(f"Invalid Camoufox addon name: {name!r}")
    if not isinstance(pin, dict):
        raise ValueError(f"Camoufox addon {name} pin must be an object")
    version = pin.get("version")
    url = pin.get("url")
    digest = pin.get("digest")
    if not isinstance(version, str) or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", version) is None:
        raise ValueError(f"Camoufox addon {name} must pin a version")
    if not isinstance(digest, str) or _SHA256_DIGEST.fullmatch(digest) is None:
        raise ValueError(f"Camoufox addon {name} must pin a sha256 digest")
    parts = urlsplit(url) if isinstance(url, str) else None
    # A version-named file is immutable upstream; aliases such as latest.xpi move with
    # every release and would break the digest pin instead of reproducing it.
    if (
        parts is None
        or parts.scheme != "https"
        or not parts.netloc
        or parts.query
        or parts.fragment
        or not parts.path.endswith(".xpi")
        or version not in Path(parts.path).name
    ):
        raise ValueError(
            f"Camoufox addon {name} must use an https .xpi URL naming version {version}"
        )
    return _AddonPin(version=version, url=url, sha256=digest.removeprefix("sha256:"))


def _load_artifact_manifest(path: Path) -> _ArtifactManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 2:
        raise ValueError("Camoufox artifact digest file must use schema version 2")
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
            or _SHA256_DIGEST.fullmatch(digest) is None
        ):
            raise ValueError(f"Invalid Camoufox artifact digest entry: {name!r}")
        digests[name] = digest.removeprefix("sha256:")

    addons = payload.get("addons")
    if not isinstance(addons, dict):
        raise ValueError("Camoufox artifact digest file must contain an addons object")
    return _ArtifactManifest(
        release_tag=release_tag,
        artifacts=digests,
        addons={name: _load_addon_pin(name, pin) for name, pin in addons.items()},
    )


def _require_pinned_default_addons(
    addons: dict[str, _AddonPin],
    default_addon_names: Iterable[str],
) -> None:
    # Camouflare launches load every Camoufox default add-on from disk and refuse to
    # start without one, so each default must be installed from a pin here.
    expected = set(default_addon_names)
    if set(addons) != expected:
        raise ValueError(
            "Camoufox addon pins must match the library's default addons exactly: "
            f"pinned {sorted(addons)}, defaults {sorted(expected)}"
        )


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


def _verify_sha256(buffer: Any, expected_digest: str, subject: str) -> None:
    buffer.seek(0)
    actual = hashlib.sha256()
    while chunk := buffer.read(1024 * 1024):
        actual.update(chunk)
    buffer.seek(0)
    actual_digest = actual.hexdigest()
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise RuntimeError(
            f"{subject} digest mismatch: "
            f"expected sha256:{expected_digest}, got sha256:{actual_digest}"
        )


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
        _verify_sha256(buffer, expected_digest, f"Camoufox artifact {artifact_name}")
        return buffer

    return download


def _install_pinned_addon(
    pin: _AddonPin,
    destination: Path,
    download: Callable[[str, Any], Any],
) -> None:
    """Replace ``destination`` with the verified add-on, leaving it untouched on failure."""
    subject = f"Camoufox addon {destination.name} {pin.version}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        staging.chmod(0o755)
        with tempfile.TemporaryFile() as archive:
            download(pin.url, archive)
            _verify_sha256(archive, pin.sha256, subject)
            with zipfile.ZipFile(archive) as extracted:
                extracted.extractall(staging)
        try:
            manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{subject} archive has no readable manifest.json") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != pin.version:
            raise RuntimeError(f"{subject} manifest.json declares a different version")
        if destination.exists():
            shutil.rmtree(destination)
        staging.replace(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    manifest = _load_artifact_manifest(DEFAULT_ARTIFACT_DIGESTS_FILE)
    release_metadata = {
        api_url: _load_release_metadata(Path(metadata_path), expected_tag=manifest.release_tag)
        for api_url, env_name in RELEASE_METADATA_FILES.items()
        if (metadata_path := os.getenv(env_name)) and Path(metadata_path).is_file()
    }

    from camoufox import pkgman
    from camoufox.__main__ import CamoufoxUpdate
    from camoufox.addons import DefaultAddons, get_addon_path

    _require_pinned_default_addons(manifest.addons, (addon.name for addon in DefaultAddons))

    def download_addon(url: str, file: Any) -> Any:
        return pkgman.webdl(url, desc="Downloading addon", buffer=file, bar=False)

    original_get = pkgman.requests.get
    original_check_asset = pkgman.CamoufoxFetcher.check_asset
    original_download = pkgman.CamoufoxFetcher.download_file
    pkgman.requests.get = _metadata_aware_get(
        original_get,
        release_metadata,
        release_tag=manifest.release_tag,
    )
    pkgman.CamoufoxFetcher.check_asset = _pinned_asset_checker(
        original_check_asset,
        manifest.artifacts,
    )
    pkgman.CamoufoxFetcher.download_file = staticmethod(
        _digest_verifying_download(original_download, manifest.artifacts)
    )

    try:
        CamoufoxUpdate().update()
        # Resolving the add-on path may reinstall the browser, so it stays inside the
        # pinned-download patches.
        for name, pin in manifest.addons.items():
            pkgman.rprint(f"Installing addon {name} {pin.version}: {pin.url}")
            _install_pinned_addon(pin, Path(get_addon_path(name)), download_addon)
    finally:
        pkgman.requests.get = original_get
        pkgman.CamoufoxFetcher.check_asset = original_check_asset
        pkgman.CamoufoxFetcher.download_file = staticmethod(original_download)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
