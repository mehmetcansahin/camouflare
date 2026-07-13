#!/usr/bin/env python3
"""Record compressed layer sizes for each platform in a pushed image index."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _inspect(reference: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", reference],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _manifest_size(manifest: dict[str, Any]) -> int:
    config = manifest.get("config", {})
    layers = manifest.get("layers", [])
    return int(config.get("size", 0)) + sum(int(layer.get("size", 0)) for layer in layers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", help="Pushed image reference, preferably name@sha256:digest")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Export amd64_digest and arm64_digest to GITHUB_OUTPUT.",
    )
    args = parser.parse_args()

    index = _inspect(args.image)
    results: dict[str, int] = {}
    digests: dict[str, str] = {}
    for descriptor in index.get("manifests", []):
        platform = descriptor.get("platform", {})
        if platform.get("os") != "linux" or platform.get("architecture") not in {"amd64", "arm64"}:
            continue
        architecture = str(platform["architecture"])
        manifest = _inspect(f"{args.image.split('@', 1)[0]}@{descriptor['digest']}")
        results[f"linux/{architecture}"] = _manifest_size(manifest)
        digests[architecture] = str(descriptor["digest"])

    if set(results) != {"linux/amd64", "linux/arm64"}:
        raise RuntimeError(f"Expected amd64 and arm64 manifests, found: {sorted(results)}")

    payload = {"image": args.image, "compressed_bytes": results, "manifest_digests": digests}
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["### Published image sizes", "", "| Platform | Compressed size |", "| --- | ---: |"]
    for platform, size in sorted(results.items()):
        lines.append(f"| {platform} | {size / (1024 * 1024):.1f} MiB |")
    rendered = "\n".join(lines) + "\n"
    print(rendered)
    if summary_path := os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write(rendered)
    if args.github_output:
        output_path = os.getenv("GITHUB_OUTPUT")
        if not output_path:
            raise RuntimeError("--github-output requires GITHUB_OUTPUT.")
        with Path(output_path).open("a", encoding="utf-8") as output_file:
            output_file.write(f"amd64_digest={digests['amd64']}\n")
            output_file.write(f"arm64_digest={digests['arm64']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
