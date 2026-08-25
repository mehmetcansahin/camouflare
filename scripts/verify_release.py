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
CHANGELOG_RELEASE_HEADING = re.compile(
    r"^## \[(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\] "
    r"- \d{4}-\d{2}-\d{2}$",
    re.MULTILINE,
)
CHANGELOG_REFERENCE = re.compile(
    r"^\[(?P<label>[^\]]+)\]: (?P<url>\S+)$",
    re.MULTILINE,
)


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


def _changelog_errors(
    changelog: str,
    *,
    tag_version: str,
    repository_url: str,
) -> list[str]:
    errors: list[str] = []
    release_versions = [
        match.group("version") for match in CHANGELOG_RELEASE_HEADING.finditer(changelog)
    ]
    if tag_version not in release_versions:
        errors.append(f"CHANGELOG.md has no dated [{tag_version}] release heading")

    references = {
        match.group("label"): match.group("url")
        for match in CHANGELOG_REFERENCE.finditer(changelog)
    }
    repository_url = repository_url.rstrip("/")
    expected_unreleased = f"{repository_url}/compare/v{tag_version}...HEAD"
    actual_unreleased = references.get("Unreleased")
    if actual_unreleased != expected_unreleased:
        errors.append(
            f"CHANGELOG.md [Unreleased] link is {actual_unreleased or 'missing'}, "
            f"expected {expected_unreleased}"
        )

    if tag_version in release_versions:
        release_index = release_versions.index(tag_version)
        if release_index + 1 < len(release_versions):
            previous_version = release_versions[release_index + 1]
            expected_release = f"{repository_url}/compare/v{previous_version}...v{tag_version}"
        else:
            expected_release = f"{repository_url}/releases/tag/v{tag_version}"
        actual_release = references.get(tag_version)
        if actual_release != expected_release:
            errors.append(
                f"CHANGELOG.md [{tag_version}] link is {actual_release or 'missing'}, "
                f"expected {expected_release}"
            )

    return errors


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
    project_urls = project.get("urls", {})
    repository_url = project_urls.get("Repository") if isinstance(project_urls, dict) else None
    if not isinstance(repository_url, str) or not repository_url:
        errors.append("pyproject.toml has no project.urls.Repository")
    else:
        errors.extend(
            _changelog_errors(
                changelog,
                tag_version=tag_version,
                repository_url=repository_url,
            )
        )

    if errors:
        for error in errors:
            print(f"release error: {error}", file=sys.stderr)
        return 1
    print(f"Release {args.tag} matches package metadata, source version, and changelog.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
