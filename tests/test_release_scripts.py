from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from camoufox.addons import DefaultAddons

from scripts import (
    check_image_size,
    check_release_destinations,
    fetch_camoufox,
    render_security_allowlist,
    report_image_sizes,
    verify_release,
)


def test_camoufox_release_metadata_wrapper_avoids_anonymous_api_request() -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    camoufox_metadata = [{"tag_name": "v-camoufox", "assets": []}]
    metadata = {
        fetch_camoufox.CAMOUFOX_RELEASES_API: camoufox_metadata,
    }

    def original_get(url: str, *args: object, **kwargs: object) -> object:
        calls.append((url, args, kwargs))
        return object()

    wrapped_get = fetch_camoufox._metadata_aware_get(original_get, metadata)
    response = wrapped_get(fetch_camoufox.CAMOUFOX_RELEASES_API, timeout=20)

    response.raise_for_status()
    assert response.json() == camoufox_metadata
    assert calls == []

    fallback = wrapped_get("https://example.com/asset.zip", timeout=30)
    assert fallback is not response
    assert calls == [("https://example.com/asset.zip", (), {"timeout": 30})]


def test_camoufox_release_metadata_file_must_be_an_object_or_array(
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "releases.json"
    metadata_path.write_text('"rate limited"', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object or array of objects"):
        fetch_camoufox._load_release_metadata(metadata_path)


def test_camoufox_release_metadata_requires_the_pinned_tag(tmp_path: Path) -> None:
    metadata_path = tmp_path / "release.json"
    metadata_path.write_text(
        json.dumps({"tag_name": "v152.0.4-beta.30", "assets": []}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"v152\.0\.4-beta\.29"):
        fetch_camoufox._load_release_metadata(
            metadata_path,
            expected_tag="v152.0.4-beta.29",
        )


def test_camoufox_release_metadata_accepts_exact_release_object(tmp_path: Path) -> None:
    metadata_path = tmp_path / "release.json"
    payload = {"tag_name": "v152.0.4-beta.29", "assets": []}
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    assert fetch_camoufox._load_release_metadata(
        metadata_path,
        expected_tag="v152.0.4-beta.29",
    ) == [payload]


def test_camoufox_release_wrapper_fetches_exact_tag_without_metadata() -> None:
    calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
    payload = {"tag_name": "v152.0.4-beta.29", "assets": []}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return payload

    def original_get(url: str, *args: object, **kwargs: object) -> Response:
        calls.append((url, args, kwargs))
        return Response()

    wrapped_get = fetch_camoufox._metadata_aware_get(
        original_get,
        {},
        release_tag="v152.0.4-beta.29",
    )
    response = wrapped_get(fetch_camoufox.CAMOUFOX_RELEASES_API, timeout=20)

    assert response.json() == [payload]
    assert calls == [
        (
            f"{fetch_camoufox.CAMOUFOX_RELEASE_TAGS_API}/v152.0.4-beta.29",
            (),
            {"timeout": 20},
        )
    ]


def test_camoufox_artifact_digest_file_pins_current_release() -> None:
    manifest = fetch_camoufox._load_artifact_manifest(fetch_camoufox.DEFAULT_ARTIFACT_DIGESTS_FILE)

    assert manifest.release_tag == "v152.0.4-beta.29"
    assert manifest.artifacts["camoufox-152.0.4-beta.29-lin.x86_64.zip"] == (
        "1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c"
    )
    assert "camoufox-152.0.4-beta.30-lin.x86_64.zip" not in manifest.artifacts
    fetch_camoufox._require_pinned_default_addons(
        manifest.addons,
        (addon.name for addon in DefaultAddons),
    )


def test_camoufox_fetch_rejects_unpinned_default_addon() -> None:
    with pytest.raises(ValueError, match="UBO"):
        fetch_camoufox._require_pinned_default_addons({}, ["UBO"])


@pytest.mark.parametrize(
    ("pin", "message"),
    [
        pytest.param(
            {
                "version": "1.75.0",
                "url": DefaultAddons.UBO.value,
                "digest": f"sha256:{'0' * 64}",
            },
            "naming version 1.75.0",
            id="floating-latest-url",
        ),
        pytest.param(
            {"version": "1.75.0", "url": "https://example.com/ublock_origin-1.75.0.xpi"},
            "sha256 digest",
            id="missing-digest",
        ),
    ],
)
def test_camoufox_addon_pin_rejects_floating_or_unverified_download(
    tmp_path: Path,
    pin: dict[str, str],
    message: str,
) -> None:
    manifest = json.loads(fetch_camoufox.DEFAULT_ARTIFACT_DIGESTS_FILE.read_text(encoding="utf-8"))
    manifest["addons"]["UBO"] = pin
    manifest_path = tmp_path / "camoufox-artifacts.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        fetch_camoufox._load_artifact_manifest(manifest_path)


def _xpi(manifest: dict[str, str] | None) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as xpi:
        if manifest is not None:
            xpi.writestr("manifest.json", json.dumps(manifest))
        xpi.writestr("background.js", "")
    return archive.getvalue()


def _serving(payload: bytes) -> Callable[[str, Any], None]:
    def download(_url: str, file: Any) -> None:
        file.write(payload)

    return download


def _addon_pin(reviewed: bytes) -> fetch_camoufox._AddonPin:
    return fetch_camoufox._AddonPin(
        version="1.75.0",
        url="https://example.com/ublock_origin-1.75.0.xpi",
        sha256=hashlib.sha256(reviewed).hexdigest(),
    )


def test_camoufox_addon_install_replaces_stale_path_with_verified_archive(
    tmp_path: Path,
) -> None:
    payload = _xpi({"version": "1.75.0"})
    destination = tmp_path / "addons" / "UBO"
    # Upstream `camoufox fetch` leaves this directory empty when the download fails.
    destination.mkdir(parents=True)

    fetch_camoufox._install_pinned_addon(_addon_pin(payload), destination, _serving(payload))

    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.75.0"
    assert [path.name for path in destination.parent.iterdir()] == ["UBO"]


@pytest.mark.parametrize(
    ("served", "reviewed", "message"),
    [
        pytest.param(
            _xpi({"version": "1.75.0"}),
            b"reviewed archive",
            "digest mismatch",
            id="digest-mismatch",
        ),
        pytest.param(_xpi(None), None, "no readable manifest.json", id="missing-manifest"),
        pytest.param(
            _xpi({"version": "1.76.0"}),
            None,
            "different version",
            id="different-version",
        ),
    ],
)
def test_camoufox_addon_install_failure_leaves_no_addon_path(
    tmp_path: Path,
    served: bytes,
    reviewed: bytes | None,
    message: str,
) -> None:
    destination = tmp_path / "addons" / "UBO"
    pin = _addon_pin(served if reviewed is None else reviewed)

    with pytest.raises(RuntimeError, match=message):
        fetch_camoufox._install_pinned_addon(pin, destination, _serving(served))

    assert list(destination.parent.iterdir()) == []


def test_camoufox_fetch_fails_when_pinned_addon_download_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import camoufox.__main__ as camoufox_cli
    from camoufox import addons, pkgman

    manifest = fetch_camoufox._load_artifact_manifest(fetch_camoufox.DEFAULT_ARTIFACT_DIGESTS_FILE)
    requested: list[str] = []

    class PinnedBrowserUpdate:
        def update(self) -> None:
            return None

    def unreachable(url: str, **_kwargs: object) -> None:
        requested.append(url)
        raise OSError(f"cannot reach {url}")

    monkeypatch.delenv("CAMOUFLARE_CAMOUFOX_RELEASES_FILE", raising=False)
    monkeypatch.setattr(camoufox_cli, "CamoufoxUpdate", PinnedBrowserUpdate)
    monkeypatch.setattr(addons, "get_addon_path", lambda name: str(tmp_path / "addons" / name))
    monkeypatch.setattr(pkgman, "webdl", unreachable)

    with pytest.raises(OSError, match="cannot reach"):
        fetch_camoufox.main()

    assert requested == [manifest.addons["UBO"].url]
    assert list((tmp_path / "addons").iterdir()) == []


def test_camoufox_fetcher_selects_only_pinned_assets() -> None:
    def original_check_asset(_fetcher: object, asset: dict[str, object]) -> str:
        return str(asset["browser_download_url"])

    checker = fetch_camoufox._pinned_asset_checker(
        original_check_asset,
        {"pinned.zip": "0" * 64},
    )

    assert checker(object(), {"name": "newer.zip", "browser_download_url": "newer"}) is None
    assert checker(object(), {"name": "pinned.zip", "browser_download_url": "pinned"}) == "pinned"


def test_camoufox_download_verifies_bytes_and_rewinds_buffer() -> None:
    payload = b"pinned Camoufox archive"
    expected = hashlib.sha256(payload).hexdigest()

    def original_download(file: io.BytesIO, _url: str) -> io.BytesIO:
        file.write(payload)
        file.seek(0)
        return file

    download = fetch_camoufox._digest_verifying_download(
        original_download,
        {"camoufox.zip": expected},
    )

    buffer = download(
        io.BytesIO(),
        "https://github.com/daijro/camoufox/releases/download/pinned/camoufox.zip",
    )

    assert buffer.read() == payload


def test_camoufox_download_rejects_digest_mismatch() -> None:
    def original_download(file: io.BytesIO, _url: str) -> io.BytesIO:
        file.write(b"unexpected bytes")
        file.seek(0)
        return file

    download = fetch_camoufox._digest_verifying_download(
        original_download,
        {"camoufox.zip": "0" * 64},
    )

    with pytest.raises(RuntimeError, match="digest mismatch"):
        download(io.BytesIO(), "https://example.com/camoufox.zip")


def test_release_verifier_accepts_exact_tag_and_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_release.py", "v2.0.0"])
    assert verify_release.main() == 0

    monkeypatch.setattr(sys, "argv", ["verify_release.py", "v2.0.1"])
    assert verify_release.main() == 1


def test_release_verifier_rejects_stale_or_missing_changelog_links() -> None:
    repository_url = "https://github.com/mehmetcansahin/camouflare"
    changelog = f"""\
## [Unreleased]

## [1.3.2] - 2026-08-25

## [1.3.1] - 2026-07-26

[Unreleased]: {repository_url}/compare/v1.3.2...HEAD
[1.3.2]: {repository_url}/compare/v1.3.1...v1.3.2
[1.3.1]: {repository_url}/releases/tag/v1.3.1
"""
    assert (
        verify_release._changelog_errors(
            changelog,
            tag_version="1.3.2",
            repository_url=repository_url,
        )
        == []
    )

    stale_changelog = changelog.replace(
        f"{repository_url}/compare/v1.3.2...HEAD",
        f"{repository_url}/compare/v1.3.1...HEAD",
    ).replace(
        f"[1.3.2]: {repository_url}/compare/v1.3.1...v1.3.2\n",
        "",
    )
    errors = verify_release._changelog_errors(
        stale_changelog,
        tag_version="1.3.2",
        repository_url=repository_url,
    )

    assert len(errors) == 2
    assert "[Unreleased] link" in errors[0]
    assert "v1.3.2...HEAD" in errors[0]
    assert "[1.3.2] link is missing" in errors[1]


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {
                "id": "CVE-2026-1",
                "reason": "A sufficiently specific reason",
                "expires_on": "2026-01-01",
            },
            "expired",
        ),
        (
            {"id": "CVE-2026-1", "reason": "too short", "expires_on": "2099-01-01"},
            "specific reason",
        ),
        (
            {
                "id": "CVE-2026-1\nCVE-2026-2",
                "reason": "A sufficiently specific reason",
                "expires_on": "2099-01-01",
            },
            "one token",
        ),
    ],
)
def test_security_allowlist_rejects_expired_or_unsafe_exceptions(
    item: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        render_security_allowlist._validate([item], today=date(2026, 7, 11))


def test_security_allowlist_renders_only_active_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "allowlist.json"
    output = tmp_path / "generated" / ".trivyignore"
    source.write_text(
        json.dumps(
            {
                "version": 1,
                "exceptions": [
                    {
                        "id": "CVE-2099-0001",
                        "reason": "Upstream fix is scheduled and risk is isolated.",
                        "expires_on": (date.today() + timedelta(days=1)).isoformat(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["render_security_allowlist.py", "--source", str(source), "--output", str(output)],
    )

    assert render_security_allowlist.main() == 0
    assert output.read_text(encoding="utf-8") == "CVE-2099-0001\n"


@pytest.mark.parametrize(
    ("current", "expected_status"),
    [(110, "ok"), (111, "warning")],
)
def test_image_size_warning_uses_strictly_greater_than_ten_percent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    current: int,
    expected_status: str,
) -> None:
    output = tmp_path / f"size-{current}.json"
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_image_size.py",
            "--current",
            str(current),
            "--baseline",
            "100",
            "--threshold",
            "0.10",
            "--output",
            str(output),
        ],
    )

    assert check_image_size.main() == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == expected_status


def test_image_report_requires_both_release_platforms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    index = {
        "manifests": [
            {
                "digest": "sha256:amd64",
                "platform": {"os": "linux", "architecture": "amd64"},
            }
        ]
    }
    manifest = {"config": {"size": 10}, "layers": [{"size": 20}]}
    monkeypatch.setattr(
        report_image_sizes,
        "_inspect",
        lambda reference: index if reference == "example/image@sha256:index" else manifest,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "report_image_sizes.py",
            "example/image@sha256:index",
            "--output",
            str(tmp_path / "sizes.json"),
        ],
    )

    with pytest.raises(RuntimeError, match="amd64 and arm64"):
        report_image_sizes.main()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (subprocess.CompletedProcess([], 0, stdout="manifest", stderr=""), True),
        (
            subprocess.CompletedProcess([], 1, stdout="", stderr="manifest unknown"),
            False,
        ),
    ],
)
def test_release_destination_image_check_has_definitive_results(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str],
    expected: bool,
) -> None:
    monkeypatch.setattr(
        check_release_destinations.subprocess,
        "run",
        lambda *_args, **_kwargs: result,
    )

    assert check_release_destinations._image_tag_exists("ghcr.io/example/image:1.0.0") is expected


def test_release_destination_image_check_rejects_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="connection timed out")
    monkeypatch.setattr(
        check_release_destinations.subprocess,
        "run",
        lambda *_args, **_kwargs: result,
    )

    with pytest.raises(RuntimeError, match="without a definitive"):
        check_release_destinations._image_tag_exists("ghcr.io/example/image:1.0.0")


@pytest.mark.parametrize("digest", [None, f"sha256:{'a' * 64}"])
def test_release_destination_writes_ghcr_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    digest: str | None,
) -> None:
    github_output = tmp_path / "github-output"
    references: list[str] = []
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))

    def image_digest(reference: str) -> str | None:
        references.append(reference)
        return digest

    monkeypatch.setattr(check_release_destinations, "_image_tag_digest", image_digest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_release_destinations.py",
            "--tag",
            "v1.0.0",
            "--image",
            "ghcr.io/example/camouflare",
            "--github-output",
        ],
    )

    assert check_release_destinations.main() == 0
    assert references == ["ghcr.io/example/camouflare:1.0.0"]
    output = github_output.read_text(encoding="utf-8")
    assert f"image_exists={str(digest is not None).lower()}" in output
    assert f"image_digest={digest or ''}" in output
    assert "pypi" not in output.lower()


def test_image_tag_digest_parses_exact_index_and_rejects_missing_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = f"sha256:{'a' * 64}"
    success = subprocess.CompletedProcess(
        [],
        0,
        stdout=f"Name: ghcr.io/example/image:1.0.0\nDigest: {digest}\n",
        stderr="",
    )
    monkeypatch.setattr(
        check_release_destinations.subprocess,
        "run",
        lambda *_args, **_kwargs: success,
    )
    assert check_release_destinations._image_tag_digest("ghcr.io/example/image:1.0.0") == digest

    malformed = subprocess.CompletedProcess([], 0, stdout="Name: image\n", stderr="")
    monkeypatch.setattr(
        check_release_destinations.subprocess,
        "run",
        lambda *_args, **_kwargs: malformed,
    )
    with pytest.raises(RuntimeError, match="parseable index digest"):
        check_release_destinations._image_tag_digest("ghcr.io/example/image:1.0.0")
