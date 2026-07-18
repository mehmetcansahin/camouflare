from __future__ import annotations

import re
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path

from camouflare import __version__
from camouflare.documentation import DOCUMENTATION_HTML

ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_guarded_default_solver() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "`GET /health` returns process liveness" in readme
    assert "does not read browser state" in readme
    assert "`GET /ready` checks that the browser pool can create a page" in readme
    assert "| `CHALLENGE_SOLVER` | `none` |" in readme
    assert "| `HOST` | `127.0.0.1` |" in readme
    assert "| `CAMOUFLARE_API_TOKEN` | unset |" in readme
    assert "Send either `Authorization: Bearer <token>` or" in readme
    assert "`X-API-Token: <token>`" in readme
    assert "CHALLENGE_SOLVER=click uv run python -m camouflare" in readme
    assert "Authorization: Bearer" in readme
    assert "127.0.0.1:8191:8191" in readme
    assert "CAMOUFLARE_API_TOKEN:?Set CAMOUFLARE_API_TOKEN" in compose
    assert "change-me" not in readme
    assert "change-me" not in compose
    assert "Use Camouflare only on systems you own" in readme
    assert "does not accept requests to bypass a specific third-party" in readme
    assert "is not published to PyPI" in readme
    assert "ghcr.io/mehmetcansahin/camouflare:1.2.0" in readme
    assert "ghcr.io/mehmetcansahin/camouflare:1.2.0" in compose
    assert "git clone https://github.com/mehmetcansahin/camouflare.git" in readme
    assert "python -m pip install ." in readme
    assert 'python -m pip install "camouflare==1.2.0"' not in readme
    assert "docker compose up --build" in readme
    assert "CAMOUFOX_GEOIP" not in readme
    assert "CAMOUFOX_GEOIP" not in compose


def test_documentation_html_matches_guarded_default_solver() -> None:
    assert "<code>/ready</code>" in DOCUMENTATION_HTML
    assert "<code>/diagnostics</code>" in DOCUMENTATION_HTML
    assert "browser-readiness" in DOCUMENTATION_HTML
    assert "lightweight liveness" in DOCUMENTATION_HTML
    assert "<code>CHALLENGE_SOLVER</code>" in DOCUMENTATION_HTML
    assert "<td><code>none</code></td>" in DOCUMENTATION_HTML
    assert "<code>CAMOUFLARE_API_TOKEN</code>" in DOCUMENTATION_HTML
    assert "<td><code>127.0.0.1</code></td>" in DOCUMENTATION_HTML
    assert "Authorization: Bearer" in DOCUMENTATION_HTML
    assert "X-API-Token" in DOCUMENTATION_HTML
    assert "change-me" not in DOCUMENTATION_HTML
    assert "enabled explicitly with <code>CHALLENGE_SOLVER=click</code>" in DOCUMENTATION_HTML
    assert "out of scope for this project" in DOCUMENTATION_HTML
    assert "CAMOUFOX_GEOIP" not in DOCUMENTATION_HTML


def test_open_source_metadata_files_are_present() -> None:
    for filename in (
        "LICENSE",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
    ):
        path = ROOT / filename
        assert path.is_file(), f"{filename} is missing"
        assert path.read_text(encoding="utf-8").strip()


def test_pyproject_includes_public_package_metadata() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert metadata["license"] == "Apache-2.0"
    assert metadata["license-files"] == ["LICENSE"]
    assert metadata["authors"] == [{"name": "Mehmetcan"}]
    assert metadata["maintainers"] == [{"name": "Mehmetcan"}]
    assert metadata["urls"]["Repository"] == ("https://github.com/mehmetcansahin/camouflare")
    assert "License :: OSI Approved :: Apache Software License" not in metadata["classifiers"]
    assert "camoufox>=0.4,<0.5" in metadata["dependencies"]
    assert not any(dependency.startswith("camoufox[") for dependency in metadata["dependencies"])


def test_public_files_use_canonical_repository_owner() -> None:
    public_files = (
        ".github/ISSUE_TEMPLATE/config.yml",
        "CHANGELOG.md",
        "README.md",
        "compose.yaml",
        "docs/rollback.md",
        "docs/upgrade-to-1.0.md",
        "pyproject.toml",
    )
    legacy_repository = "mehmetcan" + "/camouflare"

    for relative_path in public_files:
        contents = (ROOT / relative_path).read_text(encoding="utf-8")
        assert legacy_repository not in contents, f"{relative_path} uses the old owner"


def test_release_version_has_one_authoritative_source() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["dynamic"] == ["version"]
    assert metadata["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "camouflare._version.__version__"
    }
    assert installed_version("camouflare") == __version__ == "1.2.0"


def test_ci_runs_supported_python_matrix_and_builds_package() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "permissions:" in workflow
    assert "contents: read" in workflow
    assert "python-version:" in workflow
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert version in workflow
    assert "uv build" in workflow


def test_github_actions_invoke_pytest_as_a_module() -> None:
    for workflow_path in (ROOT / ".github" / "workflows").glob("*.yml"):
        lines = workflow_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if "uv run" in line and "pytest" in line:
                assert "python -m pytest" in line, (
                    f"{workflow_path.name}:{line_number} invokes pytest directly"
                )


def test_github_actions_avoid_anonymous_camoufox_release_api_calls() -> None:
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in (ROOT / ".github" / "workflows").glob("*.yml")
    }

    for workflow_name in ("ci.yml", "nightly.yml", "release.yml"):
        workflow = workflows[workflow_name]
        assert "camoufox fetch" not in workflow
        assert "gh api repos/daijro/camoufox/releases" in workflow
        assert "P3TERX/GeoLite.mmdb" not in workflow
        assert "geolite_releases" not in workflow
        assert "scripts/fetch_camoufox.py" in workflow

    for workflow_name in ("ci.yml", "release.yml"):
        assert (
            "camoufox_releases=${{ runner.temp }}/camoufox-releases.json"
            in workflows[workflow_name]
        )


def test_all_github_actions_are_pinned_to_full_commit_shas() -> None:
    for workflow_path in (ROOT / ".github" / "workflows").glob("*.yml"):
        for line in workflow_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("uses:"):
                continue
            action = stripped.removeprefix("uses:").split("#", 1)[0].strip()
            assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}", action), (
                f"{workflow_path.name} has an unpinned action: {action}"
            )


def test_ci_and_nightly_cover_real_browser_container_and_soak_gates() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    nightly = (ROOT / ".github/workflows/nightly.yml").read_text(encoding="utf-8")

    assert "arch: [amd64, arm64]" in ci
    assert "CAMOUFLARE_RUN_BROWSER_TESTS" in ci
    assert "scripts/container_smoke.sh" in ci
    assert (
        "CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS: "
        "${{ matrix.arch == 'arm64' && '120000' || '30000' }}"
    ) in ci
    assert (
        "CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS: "
        "${{ matrix.arch == 'arm64' && '120000' || '15000' }}"
    ) in ci
    assert (
        "CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS: ${{ matrix.arch == 'arm64' && '120000' || '60000' }}"
    ) in ci
    assert (
        "CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS: ${{ matrix.arch == 'arm64' && '180' || '90' }}"
    ) in ci
    assert (
        "CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS: ${{ matrix.arch == 'arm64' && '180' || '120' }}"
    ) in ci
    assert "--cov-fail-under=85" in ci
    assert "ruff format --check" in ci
    assert "pyright==1.1.411" in ci
    assert "Five-minute real-browser soak" in nightly
    assert 'CAMOUFLARE_SOAK_REQUESTS: "100"' in nightly
    assert 'CAMOUFLARE_SOAK_DURATION_SECONDS: "300"' in nightly
    assert 'CAMOUFLARE_SOAK_WARMUP_REQUESTS: "100"' in nightly
    assert 'CAMOUFLARE_SOAK_BROWSER_MAX_USES: "20"' in nightly
    assert 'CAMOUFLARE_SOAK_REQUEST_TIMEOUT_MS: "60000"' in nightly
    assert 'CAMOUFLARE_SOAK_SETTLE_SECONDS: "5"' in nightly
    assert "runs-on: macos-15" in nightly
    assert "SMOKE_URL" in nightly


def test_release_is_immutable_approval_gated_and_multi_arch() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch" not in release
    assert "ref: ${{ github.sha }}" in release
    assert "Confirm the protected tag still identifies this commit" in release
    assert "python scripts/verify_release.py" in release
    assert "environment:\n      name: release" in release
    assert "linux/amd64,linux/arm64" in release
    assert (
        "CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS: "
        "${{ matrix.arch == 'arm64' && '120000' || '30000' }}"
    ) in release
    assert (
        "CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS: "
        "${{ matrix.arch == 'arm64' && '120000' || '15000' }}"
    ) in release
    assert (
        "CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS: ${{ matrix.arch == 'arm64' && '120000' || '60000' }}"
    ) in release
    assert (
        "CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS: ${{ matrix.arch == 'arm64' && '180' || '90' }}"
    ) in release
    assert (
        "CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS: ${{ matrix.arch == 'arm64' && '180' || '120' }}"
    ) in release
    assert 'CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS: "120000"' in release
    assert 'CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS: "120000"' in release
    assert 'CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS: "120000"' in release
    assert 'CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS: "180"' in release
    assert 'CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS: "180"' in release
    assert "severity: HIGH,CRITICAL" in release
    assert "attest-build-provenance" in release
    assert "sbom" in release.lower()
    assert "gh-action-pypi-publish" not in release
    assert "pypi_complete" not in release


def test_container_smoke_forwards_bounded_startup_timeouts() -> None:
    smoke = (ROOT / "scripts/container_smoke.sh").read_text(encoding="utf-8")

    assert "${CAMOUFLARE_SMOKE_POOL_ACQUIRE_TIMEOUT_MS:-30000}" in smoke
    assert "${CAMOUFLARE_SMOKE_READINESS_TIMEOUT_MS:-15000}" in smoke
    assert "${CAMOUFLARE_SMOKE_REQUEST_TIMEOUT_MS:-60000}" in smoke
    assert "${CAMOUFLARE_SMOKE_CURL_TIMEOUT_SECONDS:-90}" in smoke
    assert "${CAMOUFLARE_SMOKE_STARTUP_TIMEOUT_SECONDS:-120}" in smoke
    assert '--env POOL_ACQUIRE_TIMEOUT_MS="${pool_acquire_timeout_ms}"' in smoke
    assert '--env READINESS_TIMEOUT_MS="${readiness_timeout_ms}"' in smoke
    assert 'seq 1 "${startup_timeout_seconds}"' in smoke
    assert '\\"maxTimeout\\":${request_timeout_ms}' in smoke
    assert '--max-time "${curl_timeout_seconds}"' in smoke
    assert smoke.count("--fail-with-body") == 2
