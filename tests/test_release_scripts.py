from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError

import pytest

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
    geolite_metadata = [{"tag_name": "v-geolite", "assets": []}]
    metadata = {
        fetch_camoufox.CAMOUFOX_RELEASES_API: camoufox_metadata,
        fetch_camoufox.GEOLITE_RELEASES_API: geolite_metadata,
    }

    def original_get(url: str, *args: object, **kwargs: object) -> object:
        calls.append((url, args, kwargs))
        return object()

    wrapped_get = fetch_camoufox._metadata_aware_get(original_get, metadata)
    response = wrapped_get(fetch_camoufox.CAMOUFOX_RELEASES_API, timeout=20)

    response.raise_for_status()
    assert response.json() == camoufox_metadata
    assert wrapped_get(fetch_camoufox.GEOLITE_RELEASES_API).json() == geolite_metadata
    assert calls == []

    fallback = wrapped_get("https://example.com/asset.zip", timeout=30)
    assert fallback is not response
    assert calls == [("https://example.com/asset.zip", (), {"timeout": 30})]


def test_camoufox_release_metadata_file_must_be_an_array(
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "releases.json"
    metadata_path.write_text('{"message":"rate limited"}', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON array of objects"):
        fetch_camoufox._load_release_metadata(metadata_path)


def test_release_verifier_accepts_exact_tag_and_rejects_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["verify_release.py", "v1.0.0"])
    assert verify_release.main() == 0

    monkeypatch.setattr(sys, "argv", ["verify_release.py", "v1.0.1"])
    assert verify_release.main() == 1


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


def test_release_destination_pypi_check_distinguishes_exists_missing_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(check_release_destinations, "urlopen", lambda *_args, **_kwargs: Response())
    assert check_release_destinations._pypi_version_exists("camouflare", "1.0.0") is True

    def raise_http(code: int) -> None:
        raise HTTPError("https://pypi.invalid", code, "error", hdrs=None, fp=None)

    monkeypatch.setattr(
        check_release_destinations,
        "urlopen",
        lambda *_args, **_kwargs: raise_http(404),
    )
    assert check_release_destinations._pypi_version_exists("camouflare", "1.0.0") is False

    monkeypatch.setattr(
        check_release_destinations,
        "urlopen",
        lambda *_args, **_kwargs: raise_http(500),
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        check_release_destinations._pypi_version_exists("camouflare", "1.0.0")


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


def test_pypi_distribution_probe_reads_filenames_and_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "urls": [
            {
                "filename": "camouflare-1.0.0-py3-none-any.whl",
                "digests": {"sha256": "a" * 64},
            },
            {
                "filename": "camouflare-1.0.0.tar.gz",
                "digests": {"sha256": "b" * 64},
            },
        ]
    }
    monkeypatch.setattr(
        check_release_destinations,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode()),
    )

    assert check_release_destinations._pypi_distributions("camouflare", "1.0.0") == {
        "camouflare-1.0.0-py3-none-any.whl": "a" * 64,
        "camouflare-1.0.0.tar.gz": "b" * 64,
    }


def test_release_destination_accepts_only_a_matching_pypi_subset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "camouflare-1.0.0-py3-none-any.whl"
    sdist = dist / "camouflare-1.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    published_subset = {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()}
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(
        check_release_destinations,
        "_pypi_distributions",
        lambda _project, _version: published_subset,
    )
    monkeypatch.setattr(
        check_release_destinations,
        "_image_tag_digest",
        lambda _reference: None,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_release_destinations.py",
            "--tag",
            "v1.0.0",
            "--image",
            "ghcr.io/example/camouflare",
            "--dist-dir",
            str(dist),
            "--github-output",
        ],
    )

    assert check_release_destinations.main() == 0
    assert "pypi_complete=false" in github_output.read_text(encoding="utf-8")

    monkeypatch.setattr(
        check_release_destinations,
        "_pypi_distributions",
        lambda _project, _version: {wheel.name: "0" * 64},
    )
    assert check_release_destinations.main() == 1


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
