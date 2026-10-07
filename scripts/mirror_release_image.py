#!/usr/bin/env python3
"""Copy an exact release index between registries without replacing an existing version."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from scripts.check_release_destinations import _image_tag_digest


def mirror_release_image(
    *, tag: str, source_image: str, digest: str, destination_image: str
) -> None:
    if re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag) is None:
        raise ValueError("Expected an exact vMAJOR.MINOR.PATCH release tag.")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("Expected an exact sha256 image index digest.")

    source = f"{source_image}@{digest}"
    destination = f"{destination_image}:{tag.removeprefix('v')}"
    source_tag = f"{source_image}:{tag.removeprefix('v')}"
    if _image_tag_digest(source_tag) != digest:
        raise RuntimeError("Source release tag did not return the requested release digest.")

    existing_digest = _image_tag_digest(destination)
    if existing_digest is not None:
        if existing_digest != digest:
            raise RuntimeError(f"Refusing to replace {destination}: it names {existing_digest}.")
        print(f"{destination} already names {digest}; no copy needed.")
        return

    # A single index source retains both platform manifests and the attached BuildKit
    # SBOM/provenance manifests. Do not rebuild or add annotations that change its digest.
    subprocess.run(
        ["docker", "buildx", "imagetools", "create", "--tag", destination, source],
        check=True,
    )
    if _image_tag_digest(destination) != digest:
        raise RuntimeError(f"Copied {destination} does not match the source release digest.")
    print(f"Verified {destination}@{digest}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Exact vMAJOR.MINOR.PATCH release tag")
    parser.add_argument(
        "--source-image", required=True, help="Source image name without tag or digest"
    )
    parser.add_argument("--digest", required=True, help="Verified source image index digest")
    parser.add_argument(
        "--destination-image", required=True, help="Destination image name without tag"
    )
    args = parser.parse_args()
    try:
        mirror_release_image(
            tag=args.tag,
            source_image=args.source_image,
            digest=args.digest,
            destination_image=args.destination_image,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"release mirror error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
