#!/usr/bin/env python3
"""Inspect the immutable GHCR release destination."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

IMAGE_DIGEST = re.compile(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)
ABSENT_IMAGE_MARKERS = (
    "manifest unknown",
    "name unknown",
    "repository name not known",
    "no such manifest",
)


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


def _write_github_output(*, image_digest: str | None) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("--github-output requires GITHUB_OUTPUT.")
    with Path(output_path).open("a", encoding="utf-8") as output:
        output.write(f"image_exists={str(image_digest is not None).lower()}\n")
        output.write(f"image_digest={image_digest or ''}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="vMAJOR.MINOR.PATCH release tag")
    parser.add_argument("--image", required=True, help="GHCR image name without a tag")
    parser.add_argument("--github-output", action="store_true")
    args = parser.parse_args()

    version = args.tag.removeprefix("v")
    try:
        image_digest = _image_tag_digest(f"{args.image}:{version}")
        if args.github_output:
            _write_github_output(image_digest=image_digest)
    except (OSError, RuntimeError) as exc:
        print(f"release destination check error: {exc}", file=sys.stderr)
        return 1

    image_state = image_digest or "unused"
    print(f"Release destination state: GHCR={image_state}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
