from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import camouflare.__main__ as cli
from camouflare.config import Settings

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def _instructions() -> list[tuple[str, str]]:
    """Return Dockerfile instructions with line continuations joined."""

    instructions: list[tuple[str, str]] = []
    pending = ""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        # Docker drops blank and comment lines, including inside a continuation.
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped.removesuffix("\\") + " "
            continue
        keyword, _, argument = (pending + stripped).partition(" ")
        pending = ""
        instructions.append((keyword.upper(), argument.strip()))
    return instructions


def _arguments(keyword: str) -> list[str]:
    return [argument for name, argument in _instructions() if name == keyword]


def test_dockerfile_uses_pinned_lts_base_image() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "FROM ubuntu:24.04@sha256:" in dockerfile
    assert "alpine" not in dockerfile.lower()
    assert "ubuntu:latest" not in dockerfile


def test_dockerfile_is_single_runtime_stage() -> None:
    dockerfile = DOCKERFILE.read_text()
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert len(from_lines) == 1
    assert from_lines[0].startswith("FROM ubuntu:24.04@sha256:")
    assert " AS " not in dockerfile
    assert "COPY --from=" not in dockerfile
    assert "--target test" not in dockerfile
    assert "--target smoke" not in dockerfile


def test_dockerfile_healthcheck_uses_ipv4_loopback_and_configured_port() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "http://127.0.0.1:" in dockerfile
    assert "+ '/health'" in dockerfile
    assert "os.environ.get('PORT', '8191')" in dockerfile
    assert "http://localhost:" not in dockerfile


def test_stop_signal_reaches_only_the_python_process() -> None:
    # Docker uses the last ENTRYPOINT/CMD; both must be exec form so no shell sits
    # between dumb-init and the app and absorbs the forwarded signal.
    entrypoint = json.loads(_arguments("ENTRYPOINT")[-1])
    command = json.loads(_arguments("CMD")[-1])
    init, *init_arguments = entrypoint
    separator = init_arguments.index("--") if "--" in init_arguments else len(init_arguments)
    init_options = init_arguments[:separator]
    child = init_arguments[separator + 1 :] + command

    assert Path(init).name == "dumb-init"
    # Without single-child mode dumb-init signals the child's whole process group,
    # stopping the Playwright driver and Xvfb while the app is still draining.
    assert "--single-child" in init_options or "-c" in init_options
    assert child == ["/app/.venv/bin/python", "-m", "camouflare"]


def test_compose_stop_grace_outlasts_request_drain_and_shutdown_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = yaml.safe_load(COMPOSE.read_text())["services"]["camouflare"]
    monkeypatch.delenv("SHUTDOWN_TIMEOUT_SECONDS", raising=False)
    default_shutdown = Settings().shutdown_timeout_seconds
    shutdown_timeout = int(service["environment"].get("SHUTDOWN_TIMEOUT_SECONDS", default_shutdown))
    stop_grace = int(service["stop_grace_period"].removesuffix("s"))

    # Uvicorn drains requests before lifespan cleanup starts; Compose must not
    # SIGKILL the container before both have finished.
    assert stop_grace > cli._GRACEFUL_REQUEST_DRAIN_SECONDS + shutdown_timeout


def test_dockerfile_avoids_dev_dependencies_and_build_tools_at_runtime() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "uv run " not in dockerfile
    assert "/app/.venv/bin/python scripts/fetch_camoufox.py" in dockerfile
    assert "COPY scripts/camoufox-artifacts.json" in dockerfile
    assert "--mount=type=secret,id=camoufox_releases,required=false" in dockerfile
    assert "CAMOUFLARE_CAMOUFOX_RELEASES_FILE=/run/secrets/camoufox_releases" in dockerfile
    assert "geolite" not in dockerfile.lower()
    assert "/app/.venv/bin/playwright install-deps firefox" in dockerfile
    assert "rm -f /usr/local/bin/uv /usr/local/bin/uvx" in dockerfile
    assert "apt-get purge -y --auto-remove curl" in dockerfile


def test_dockerfile_runs_non_root_with_writable_runtime_paths() -> None:
    dockerfile = DOCKERFILE.read_text()
    runs = _arguments("RUN")
    venv_layer = next(index for index, run in enumerate(runs) if "uv sync" in run)

    assert _arguments("USER")[-1] == "1000"
    assert "useradd" not in dockerfile
    assert "XDG_CACHE_HOME=/cache" in dockerfile
    assert "chown -R 1000:1000 /app /cache /tmp" in runs[venv_layer]
    assert "chmod -R a+rwX /cache /tmp" in runs[venv_layer]
    # Changing ownership in a later layer would copy the whole virtualenv into it.
    assert not any("chown" in run or "chmod" in run for run in runs[venv_layer + 1 :])


def test_dockerfile_keeps_managed_python_out_of_ephemeral_tmp() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "UV_PYTHON_INSTALL_DIR=/opt/uv-python" in dockerfile
    assert 'python_target="$(readlink -f /app/.venv/bin/python)"' in dockerfile
    assert 'case "${python_target}" in /opt/uv-python/*)' in dockerfile
    assert "/app/.venv/bin/python --version" in dockerfile


def test_compose_uses_isolated_browser_context_profile() -> None:
    compose = COMPOSE.read_text()

    assert '"127.0.0.1:8191:8191"' in compose
    assert "CAMOUFLARE_API_TOKEN: ${CAMOUFLARE_API_TOKEN:?Set CAMOUFLARE_API_TOKEN}" in compose
    assert "change-me" not in compose
    assert 'POOL_MIN_BROWSERS: "2"' in compose
    assert 'POOL_MAX_BROWSERS: "2"' in compose
    assert 'POOL_MAX_CONTEXTS_PER_BROWSER: "1"' in compose
    assert 'POOL_ACQUIRE_TIMEOUT_MS: "10000"' in compose
    assert 'LOG_FORMAT: "json"' in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    assert 'shm_size: "2gb"' in compose
    assert 'mem_limit: "4g"' in compose
    assert "pids_limit: 1024" in compose
    assert "stop_grace_period: 45s" in compose
