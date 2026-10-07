from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from scripts import mirror_release_image

ROOT = Path(__file__).resolve().parents[1]
DIGEST = f"sha256:{'a' * 64}"
OTHER_DIGEST = f"sha256:{'b' * 64}"
SOURCE = f"ghcr.io/example/camouflare@{DIGEST}"
DESTINATION = "docker.io/example/camouflare:2.0.2"


def _mirror() -> None:
    mirror_release_image.mirror_release_image(
        tag="v2.0.2",
        source_image="ghcr.io/example/camouflare",
        digest=DIGEST,
        destination_image="docker.io/example/camouflare",
    )


def _stub_registry(
    monkeypatch: pytest.MonkeyPatch, digests: list[str | None | Exception]
) -> tuple[list[str], list[list[str]]]:
    results: Iterator[str | None | Exception] = iter(digests)
    inspections: list[str] = []
    copies: list[list[str]] = []

    def inspect(reference: str) -> str | None:
        inspections.append(reference)
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    def copy(command: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
        assert check
        copies.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(mirror_release_image, "_image_tag_digest", inspect)
    monkeypatch.setattr(mirror_release_image.subprocess, "run", copy)
    return inspections, copies


def test_mirror_copies_exact_index_then_verifies_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspections, copies = _stub_registry(monkeypatch, [DIGEST, None, DIGEST])

    _mirror()

    assert inspections == [SOURCE, DESTINATION, DESTINATION]
    assert copies == [["docker", "buildx", "imagetools", "create", "--tag", DESTINATION, SOURCE]]


def test_mirror_rerun_does_not_push_an_existing_matching_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspections, copies = _stub_registry(monkeypatch, [DIGEST, DIGEST])

    _mirror()

    assert inspections == [SOURCE, DESTINATION]
    assert copies == []


@pytest.mark.parametrize(
    ("digests", "message"),
    [
        ([None], "requested release digest"),
        ([OTHER_DIGEST], "requested release digest"),
        ([DIGEST, OTHER_DIGEST], "Refusing to replace"),
        ([DIGEST, RuntimeError("Registry unavailable")], "Registry unavailable"),
    ],
)
def test_mirror_rejects_missing_source_conflicts_and_ambiguous_checks_before_copy(
    monkeypatch: pytest.MonkeyPatch, digests: list[str | None | Exception], message: str
) -> None:
    _, copies = _stub_registry(monkeypatch, digests)

    with pytest.raises(RuntimeError, match=message):
        _mirror()

    assert copies == []


@pytest.mark.parametrize("copied_digest", [None, OTHER_DIGEST])
def test_mirror_fails_if_copied_index_does_not_match(
    monkeypatch: pytest.MonkeyPatch, copied_digest: str | None
) -> None:
    _, copies = _stub_registry(monkeypatch, [DIGEST, None, copied_digest])

    with pytest.raises(RuntimeError, match="does not match"):
        _mirror()

    assert len(copies) == 1


@pytest.mark.parametrize(
    ("tag", "digest"),
    [("v2.0", DIGEST), ("latest", DIGEST), ("v02.0.2", DIGEST), ("v2.0.2", "sha256:bad")],
)
def test_mirror_requires_exact_version_and_digest_before_registry_access(
    monkeypatch: pytest.MonkeyPatch, tag: str, digest: str
) -> None:
    inspections, copies = _stub_registry(monkeypatch, [])

    with pytest.raises(ValueError, match="Expected an exact"):
        mirror_release_image.mirror_release_image(
            tag=tag,
            source_image="ghcr.io/example/image",
            digest=digest,
            destination_image="docker.io/example/image",
        )

    assert inspections == copies == []


def test_mirror_cli_reports_copy_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_kwargs: str) -> None:
        raise subprocess.CalledProcessError(1, ["docker", "buildx", "imagetools", "create"])

    monkeypatch.setattr(mirror_release_image, "mirror_release_image", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mirror_release_image",
            "--tag",
            "v2.0.2",
            "--source-image",
            "ghcr.io/example/image",
            "--digest",
            DIGEST,
            "--destination-image",
            "docker.io/example/image",
        ],
    )

    assert mirror_release_image.main() == 1


def test_dockerhub_job_only_mirrors_the_successfully_published_digest() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    publish = workflow["jobs"]["publish"]
    mirror = workflow["jobs"]["publish-dockerhub"]

    assert publish["outputs"]["image-digest"] == "${{ steps.image.outputs.digest }}"
    assert mirror["needs"] == "publish"
    assert "if" not in mirror
    assert mirror["environment"]["name"] == "release"
    assert mirror["permissions"] == {"contents": "read", "packages": "read"}
    assert workflow["env"]["DOCKERHUB_IMAGE_NAME"] == "docker.io/mehmetcansahin/camouflare"
    steps = mirror["steps"]
    login = next(step for step in steps if step.get("with", {}).get("registry") == "docker.io")
    assert login["with"]["username"] == "mehmetcansahin"
    assert login["with"]["password"] == "${{ secrets.DOCKERHUB_TOKEN }}"
    copy = steps[-1]
    assert copy["env"]["SOURCE_DIGEST"] == "${{ needs.publish.outputs.image-digest }}"
    assert '--digest "${SOURCE_DIGEST}"' in copy["run"]
    assert '--destination-image "${DOCKERHUB_IMAGE_NAME}"' in copy["run"]
    assert not any(step.get("uses", "").startswith("docker/build-push-action@") for step in steps)
