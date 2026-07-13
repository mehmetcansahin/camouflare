#!/usr/bin/env python3
"""Inspect immutable release destinations and verify any existing PyPI files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

IMAGE_DIGEST = re.compile(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)
ABSENT_IMAGE_MARKERS = (
    "manifest unknown",
    "name unknown",
    "repository name not known",
    "no such manifest",
)


def _local_distributions(directory: Path) -> dict[str, str]:
    files = sorted((*directory.glob("*.whl"), *directory.glob("*.tar.gz")))
    if not files:
        raise RuntimeError(f"No wheel or source distribution found in {directory}.")
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def _pypi_distributions(project: str, version: str) -> dict[str, str] | None:
    request = Request(
        f"https://pypi.org/pypi/{project}/{version}/json",
        headers={"Accept": "application/json", "User-Agent": "camouflare-release-check"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload: dict[str, Any] = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"PyPI availability check returned HTTP {exc.code}.") from exc

    result: dict[str, str] = {}
    for item in payload.get("urls", []):
        filename = item.get("filename")
        digest = item.get("digests", {}).get("sha256")
        if not isinstance(filename, str) or not isinstance(digest, str):
            raise RuntimeError("PyPI returned release files without SHA-256 metadata.")
        result[filename] = digest
    if not result:
        raise RuntimeError("PyPI reports the version but no release files.")
    return result


def _pypi_version_exists(project: str, version: str) -> bool:
    """Compatibility probe used by focused release-helper tests."""

    request = Request(
        f"https://pypi.org/pypi/{project}/{version}/json",
        headers={"Accept": "application/json", "User-Agent": "camouflare-release-check"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.status == 200
    except HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"PyPI availability check returned HTTP {exc.code}.") from exc


def _definitively_absent_image(reference: str, message: str) -> bool:
    normalized = message.lower()
    reference_not_found = f"{reference.lower()}: not found" in normalized
    return reference_not_found or any(marker in normalized for marker in ABSENT_IMAGE_MARKERS)


def _image_tag_digest(reference: str) -> str | None:
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        match = IMAGE_DIGEST.search(result.stdout)
        if match is None:
            raise RuntimeError("GHCR returned an image without a parseable index digest.")
        return match.group(1)
    message = f"{result.stdout}\n{result.stderr}"
    if _definitively_absent_image(reference, message):
        return None
    raise RuntimeError("GHCR availability check failed without a definitive not-found result.")


def _image_tag_exists(reference: str) -> bool:
    """Compatibility probe used by focused release-helper tests."""

    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if _definitively_absent_image(reference, f"{result.stdout}\n{result.stderr}"):
        return False
    raise RuntimeError("GHCR availability check failed without a definitive not-found result.")


def _write_github_output(*, pypi_complete: bool, image_digest: str | None) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("--github-output requires GITHUB_OUTPUT.")
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"pypi_complete={str(pypi_complete).lower()}\n")
        output.write(f"image_exists={str(image_digest is not None).lower()}\n")
        output.write(f"image_digest={image_digest or ''}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="vMAJOR.MINOR.PATCH release tag")
    parser.add_argument("--image", required=True, help="GHCR image name without a tag")
    parser.add_argument("--dist-dir", type=Path, required=True)
    parser.add_argument("--project", default="camouflare", help="PyPI project name")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()

    version = args.tag.removeprefix("v")
    try:
        local_files = _local_distributions(args.dist_dir)
        published_files = _pypi_distributions(args.project, version)
        if published_files is not None:
            unexpected = set(published_files) - set(local_files)
            mismatched = {
                filename
                for filename, digest in published_files.items()
                if local_files.get(filename) != digest
            }
            if unexpected or mismatched:
                raise RuntimeError(
                    "PyPI version contains an unexpected filename or a SHA-256 digest that "
                    "differs from dist/."
                )
        pypi_complete = published_files == local_files
        image_digest = _image_tag_digest(f"{args.image}:{version}")
        if args.github_output:
            _write_github_output(
                pypi_complete=pypi_complete,
                image_digest=image_digest,
            )
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"release destination check error: {exc}", file=sys.stderr)
        return 1

    if pypi_complete:
        pypi_state = "complete"
    elif published_files:
        pypi_state = "matching partial upload"
    else:
        pypi_state = "unused"
    image_state = image_digest or "unused"
    print(f"Release destination state: PyPI={pypi_state}; GHCR={image_state}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
