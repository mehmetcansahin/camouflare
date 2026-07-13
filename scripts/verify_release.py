#!/usr/bin/env python3
"""Fail a release when its tag, package version, or changelog disagree."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER_TAG = re.compile(r"^v(?P<version>0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _source_version() -> str:
    module = ast.parse((ROOT / "camouflare" / "_version.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__version__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
    raise ValueError("camouflare/_version.py does not define a literal __version__.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag", help="Release tag in vMAJOR.MINOR.PATCH form")
    args = parser.parse_args()

    match = SEMVER_TAG.fullmatch(args.tag)
    if match is None:
        print("release error: tag must match vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 1
    tag_version = args.tag.removeprefix("v")

    source_version = _source_version()
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    package_version = project.get("version")
    if package_version is None:
        dynamic = project.get("dynamic", [])
        version_config = (
            metadata.get("tool", {}).get("setuptools", {}).get("dynamic", {}).get("version", {})
        )
        if "version" not in dynamic or version_config.get("attr") != (
            "camouflare._version.__version__"
        ):
            print(
                "release error: package version is not a literal or the approved dynamic source",
                file=sys.stderr,
            )
            return 1
        package_version = source_version
    errors: list[str] = []
    if package_version != tag_version:
        errors.append(f"pyproject.toml has {package_version}, expected {tag_version}")
    if source_version != tag_version:
        errors.append(f"camouflare.__version__ has {source_version}, expected {tag_version}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    release_heading = re.compile(
        rf"^## \[{re.escape(tag_version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", re.MULTILINE
    )
    if release_heading.search(changelog) is None:
        errors.append(f"CHANGELOG.md has no dated [{tag_version}] release heading")

    if errors:
        for error in errors:
            print(f"release error: {error}", file=sys.stderr)
        return 1
    print(f"Release {args.tag} matches package metadata, source version, and changelog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
