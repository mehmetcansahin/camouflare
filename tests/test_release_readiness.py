from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any

import pytest
import yaml

from camouflare import __version__
from camouflare.documentation import DOCUMENTATION_HTML

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "1" * 40


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
    assert "ghcr.io/mehmetcansahin/camouflare:2.0.0" in readme
    assert "ghcr.io/mehmetcansahin/camouflare:2.0.0" in compose
    assert "git clone https://github.com/mehmetcansahin/camouflare.git" in readme
    assert "python -m pip install ." in readme
    assert 'python -m pip install "camouflare==2.0.0"' not in readme
    assert "docker compose up --build" in readme
    assert "CAMOUFOX_GEOIP" not in readme
    assert "CAMOUFOX_GEOIP" not in compose


def test_current_deployment_docs_match_compose_profile() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    deployment = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
    release_checklist = (ROOT / "docs/release-checklist.md").read_text(encoding="utf-8")

    assert "pids_limit: 1024" in compose
    assert "| PIDs | 1024 |" in deployment
    assert 'POOL_MIN_BROWSERS: "2"' in compose
    assert 'POOL_MAX_BROWSERS: "2"' in compose
    assert 'POOL_MAX_CONTEXTS_PER_BROWSER: "1"' in compose
    assert "two warm browser processes with one isolated context" in readme
    assert "two browser processes warm with one context per browser" in deployment
    assert "known idle-age failure" not in release_checklist
    assert "Until this release is deployed" not in release_checklist


def test_readme_scopes_positioning_and_compatibility_claims() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "## Why Camouflare" in readme
    for differentiator in (
        "Warm, bounded capacity.",
        "Failure-aware responses.",
        "Self-healing lifecycle.",
        "Useful production signals.",
        "Measured release profile.",
        "Hardened, verifiable delivery.",
        "Small deployment surface.",
    ):
        assert differentiator in readme
    assert "all 45 load requests" in readme
    assert "all 16 lifecycle-canary requests" in readme
    assert "## FlareSolverr compatibility" in readme
    for command in (
        "request.get",
        "request.post",
        "sessions.create",
        "sessions.list",
        "sessions.destroy",
    ):
        assert f"`{command}`" in readme
    for compatibility_no_op in ("download", "returnRawHtml", "tabs_till_verify"):
        assert f"`{compatibility_no_op}`" in readme
    assert "Unknown fields are also ignored for compatibility" in readme
    assert "No named third-party client integration is" in readme
    assert "drop-in replacement" not in readme
    assert "works with Prowlarr" not in readme


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


def test_documentation_html_describes_v1_error_metadata() -> None:
    for field in ("errorCode", "retryable", "requestOutcomeUnknown", "fallbackUsed"):
        assert f"<code>{field}</code>" in DOCUMENTATION_HTML
    for error_code in (
        "INVALID_REQUEST",
        "SESSION_NOT_FOUND",
        "RESOURCE_LIMIT_EXCEEDED",
        "POOL_UNAVAILABLE",
        "REQUEST_TIMEOUT",
        "NAVIGATION_TIMEOUT",
        "BROWSER_TRANSPORT_CLOSED",
        "CHALLENGE_FAILED",
        "INTERNAL_ERROR",
    ):
        assert f"<code>{error_code}</code>" in DOCUMENTATION_HTML


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
        "docs/upgrade-to-2.0.md",
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
    assert installed_version("camouflare") == __version__ == "2.0.0"


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
        camoufox_api_calls = [
            line
            for line in workflow.splitlines()
            if "gh api" in line and "repos/daijro/camoufox/releases" in line
        ]
        assert camoufox_api_calls
        assert all("releases/tags/${camoufox_tag}" in line for line in camoufox_api_calls)
        assert 'camoufox-artifacts.json"))["release_tag"]' in workflow
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


def _publish_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["publish"]["steps"]


def _promotion_index(steps: list[dict[str, Any]]) -> int:
    promotions = [
        index
        for index, step in enumerate(steps)
        if "imagetools create" in step.get("run", "")
        or (
            step.get("uses", "").startswith("docker/build-push-action@")
            and "tags" in step.get("with", {})
        )
    ]
    assert len(promotions) == 1, "exactly one publish step may create a registry tag"
    return promotions[0]


def test_release_candidate_is_digest_only_and_tagged_after_every_gate() -> None:
    steps = _publish_steps()
    candidate = next(step for step in steps if step.get("id") == "candidate")
    manifests = next(step for step in steps if step.get("id") == "manifests")
    promotion_index = _promotion_index(steps)
    promotion = steps[promotion_index]
    outputs = dict(option.split("=", 1) for option in candidate["with"]["outputs"].split(","))
    gates: dict[str, int] = {}
    for index, step in enumerate(steps):
        run = step.get("run", "")
        action = step.get("uses", "")
        if "org.opencontainers.image.revision" in run:
            gates["revision check"] = index
        if action.startswith("actions/upload-artifact@"):
            gates["evidence upload"] = index
        for platform in ("amd64", "arm64"):
            digest = f"${{{{ steps.manifests.outputs.{platform}_digest }}}}"
            if "container_smoke.sh" in run and digest in run:
                gates[f"{platform} smoke"] = index
            if action.startswith("aquasecurity/trivy-action@") and step["with"].get(
                "image-ref", ""
            ).endswith(f"@{digest}"):
                gates[f"{platform} scan"] = index

    assert sorted(gates) == [
        "amd64 scan",
        "amd64 smoke",
        "arm64 scan",
        "arm64 smoke",
        "evidence upload",
        "revision check",
    ]

    # The candidate is pushed without any tag, so a failed gate never leaves a public name.
    assert outputs["type"] == "image"
    assert outputs["push-by-digest"] == "true"
    assert outputs["push"] == "true"
    assert "tags" not in candidate["with"]
    # Every gate precedes the tag and runs unconditionally, so a rerun that reuses an
    # already published image re-validates it before attestation.
    assert [name for name, index in gates.items() if index > promotion_index] == []
    assert [name for name, index in gates.items() if "if" in steps[index]] == []
    # The gates inspect the platform manifests of the same index that promotion tags.
    assert "${{ steps.image.outputs.digest }}" in manifests["run"]
    assert promotion["env"]["SOURCE_IMAGE"].endswith("@${{ steps.image.outputs.digest }}")
    assert candidate["if"] == promotion["if"] == "steps.destinations.outputs.image_exists != 'true'"


def _run_promotion_step(tmp_path: Path, *, tag_commit: str | None) -> tuple[int, list[str]]:
    steps = _publish_steps()
    promotion = steps[_promotion_index(steps)]
    script = promotion["run"]
    assert promotion["shell"] == "bash"
    assert "${{" not in script, "the step must read inputs only from its environment"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_calls = tmp_path / "docker-calls"
    stubs = {
        # Behaves like the GitHub API: only an authenticated request for the release tag's
        # commit succeeds, and it reports whatever commit the tag currently names.
        "curl": """#!/usr/bin/env bash
authorized=0
for argument in "$@"; do
  [[ "${argument}" == "Authorization: Bearer ${GH_TOKEN}" ]] && authorized=1
done
url="${!#}"
expected="https://api.github.com/repos/${GITHUB_REPOSITORY}/commits/${RELEASE_TAG}"
if [[ "${authorized}" != 1 || "${url}" != "${expected}" || -z "${STUB_TAG_COMMIT}" ]]; then
  echo "curl: (22) The requested URL returned error: 404" >&2
  exit 22
fi
printf '{"sha": "%s"}\\n' "${STUB_TAG_COMMIT}"
""",
        "docker": '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"${STUB_DOCKER_CALLS}"\n',
        "python3": f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n',
    }
    for name, content in stubs.items():
        stub = bin_dir / name
        stub.write_text(content, encoding="utf-8")
        stub.chmod(0o755)
    script_path = tmp_path / "promote.sh"
    script_path.write_text(script, encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "release-token",
        "GITHUB_REPOSITORY": "mehmetcansahin/camouflare",
        "GITHUB_SHA": RELEASE_COMMIT,
        "RELEASE_TAG": "v9.9.9",
        "IMAGE_TAG": "ghcr.io/mehmetcansahin/camouflare:9.9.9",
        "SOURCE_IMAGE": f"ghcr.io/mehmetcansahin/camouflare@sha256:{'a' * 64}",
        "STUB_TAG_COMMIT": tag_commit or "",
        "STUB_DOCKER_CALLS": str(docker_calls),
    }
    # GitHub runs `shell: bash` steps as `bash --noprofile --norc -eo pipefail {0}`.
    completed = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script_path)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = docker_calls.read_text(encoding="utf-8").splitlines() if docker_calls.exists() else []
    return completed.returncode, calls


def test_promotion_tags_the_scanned_digest_when_the_tag_still_names_the_commit(
    tmp_path: Path,
) -> None:
    returncode, docker_calls = _run_promotion_step(tmp_path, tag_commit=RELEASE_COMMIT)

    assert returncode == 0
    assert docker_calls == [
        "buildx imagetools create --tag ghcr.io/mehmetcansahin/camouflare:9.9.9 "
        f"ghcr.io/mehmetcansahin/camouflare@sha256:{'a' * 64}"
    ]


@pytest.mark.parametrize(
    "tag_commit",
    [pytest.param("2" * 40, id="tag-moved"), pytest.param(None, id="tag-lookup-failed")],
)
def test_promotion_refuses_to_tag_unless_the_tag_still_names_the_commit(
    tmp_path: Path, tag_commit: str | None
) -> None:
    returncode, docker_calls = _run_promotion_step(tmp_path, tag_commit=tag_commit)

    assert returncode != 0
    assert docker_calls == []


def test_container_smoke_forwards_bounded_startup_timeouts() -> None:
    smoke = (ROOT / "scripts/container_smoke.sh").read_text(encoding="utf-8")

    assert "--pids-limit 1024" in smoke
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


def _flatten(text: str) -> str:
    return " ".join(text.split())


def test_docs_state_the_real_session_and_queue_limits() -> None:
    readme = _flatten((ROOT / "README.md").read_text(encoding="utf-8"))
    deployment = _flatten((ROOT / "docs/deployment.md").read_text(encoding="utf-8"))

    for text in (readme, deployment):
        assert "one concurrent persistent session" in text
        assert "`POOL_RESERVED_TRANSIENT_CONTEXTS`" in text
        assert "`POOL_ACQUIRE_TIMEOUT_MS`" in text
        assert "`POOL_UNAVAILABLE`" in text
        assert "`MAX_SESSIONS` only caps the registry" in text
        # Sessions never queue, and stateless waiting is bounded by the acquire
        # timeout, so the docs must not promise open-ended backpressure.
        assert "queue for capacity" not in text
        # The request's own maxTimeout wraps the pool wait, so the shorter deadline
        # wins and the docs must not claim the acquire timeout applies regardless.
        assert "the earlier of `POOL_ACQUIRE_TIMEOUT_MS`" in text
        assert "`REQUEST_TIMEOUT`" in text
        assert "regardless of the caller's" not in text
        assert "independent of the caller's" not in text


def test_benchmark_claims_stay_within_a_single_recorded_run() -> None:
    changelog = _flatten((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    deployment = _flatten((ROOT / "docs/deployment.md").read_text(encoding="utf-8"))
    benchmarks = _flatten((ROOT / "docs/benchmarks/README.md").read_text(encoding="utf-8"))

    for text in (changelog, deployment, benchmarks):
        assert "repeatable request timeouts" not in text
        assert "reproducible 1.3.3 load run" not in text
    assert "timed out two requests in one of five four-client rounds" in changelog
    assert "one of five four-client rounds of a single recorded run" in deployment
    assert "not a demonstration that the shared-context profile fails repeatably" in benchmarks


def test_readme_discloses_the_direct_http_get_preflight() -> None:
    readme = _flatten((ROOT / "README.md").read_text(encoding="utf-8"))

    assert "When no proxy is configured, GET can fall back to direct HTTP" in readme
    assert "`ajax=true` GET that asks for no cookies, wait time, or screenshot" in readme
    assert "preflight response omits `fallbackUsed` because no navigation was attempted" in readme


def test_documentation_covers_every_configuration_environment_variable() -> None:
    config = (ROOT / "camouflare/config.py").read_text(encoding="utf-8")
    # Every way config.py reads an environment variable, so a setting added through
    # any of these helpers cannot slip past the check undocumented.
    names = set(re.findall(r'(?:os\.getenv|_int_env|_bool_env)\("([A-Z][A-Z_0-9]*)"', config))

    assert len(names) >= 30
    undocumented = sorted(
        name for name in names if f"<code>{name}</code>" not in DOCUMENTATION_HTML
    )

    assert undocumented == []


def test_documentation_states_session_capacity_and_the_direct_http_preflight() -> None:
    flattened = _flatten(DOCUMENTATION_HTML)

    assert "It is not the concurrency limit" in flattened
    assert "sessions.create</code> is rejected immediately with HTTP 503" in flattened
    assert "therefore allows one concurrent session" in flattened
    assert "is attempted over direct HTTP before the browser" in flattened
    assert "<code>fallbackUsed</code> is omitted because no" in flattened
    assert "Optional fields are omitted when they do not apply" in flattened
    assert "Neither the preflight nor the transport-failure fallback runs when a" in flattened
    assert "whichever deadline is shorter wins" in flattened
    assert "busy or not, stops being preferred for new leases" in flattened
    assert "including one past a recycle limit that is finishing its work" in flattened
    assert "Keep it at or above that capacity" in flattened


def test_docs_describe_the_saturated_readiness_short_circuit() -> None:
    readme = _flatten((ROOT / "README.md").read_text(encoding="utf-8"))
    deployment = _flatten((ROOT / "docs/deployment.md").read_text(encoding="utf-8"))
    flattened = _flatten(DOCUMENTATION_HTML)

    assert "`capacity_state: saturated` instead of queueing a probe" in readme
    assert "returns HTTP 200 with `capacity_state: saturated` without waiting" in deployment
    assert "A 503 therefore means the pool could not produce a working browser" in deployment
    assert "dead busy browser is probed rather than reported as saturated" in deployment
    assert "dead busy browser is probed, never reported as saturated" in flattened
    assert "<code>capacity_state</code> <code>saturated</code>" in flattened
    # The earlier wording, now false, must not come back.
    assert "so it also waits and can return 503" not in deployment


def test_docs_gate_browser_count_increases_on_a_resource_load_test() -> None:
    readme = _flatten((ROOT / "README.md").read_text(encoding="utf-8"))
    deployment = _flatten((ROOT / "docs/deployment.md").read_text(encoding="utf-8"))
    changelog = _flatten((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    assert "only after measuring container memory and PID usage" in readme
    assert "publishes no per-browser memory or PID figures" in deployment
    assert "records service-side counters only" in deployment
    assert "after load testing memory and PID usage" in changelog
