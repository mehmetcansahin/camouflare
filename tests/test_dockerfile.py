from __future__ import annotations

from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


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


def test_dockerfile_has_runtime_process_and_healthcheck_tools() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "dumb-init" in dockerfile
    assert "curl -fsS" not in dockerfile
    assert "/app/.venv/bin/python -c" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/dumb-init", "--"]' in dockerfile
    assert 'CMD ["/app/.venv/bin/python", "-m", "camouflare"]' in dockerfile


def test_dockerfile_avoids_dev_dependencies_and_build_tools_at_runtime() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "uv run " not in dockerfile
    assert "/app/.venv/bin/python scripts/fetch_camoufox.py" in dockerfile
    assert "--mount=type=secret,id=camoufox_releases,required=false" in dockerfile
    assert "CAMOUFLARE_CAMOUFOX_RELEASES_FILE=/run/secrets/camoufox_releases" in dockerfile
    assert "geolite" not in dockerfile.lower()
    assert "/app/.venv/bin/playwright install-deps firefox" in dockerfile
    assert "rm -f /usr/local/bin/uv /usr/local/bin/uvx" in dockerfile
    assert "apt-get purge -y --auto-remove curl" in dockerfile


def test_dockerfile_runs_non_root_with_writable_runtime_paths() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "USER 1000" in dockerfile
    assert "useradd" not in dockerfile
    assert "XDG_CACHE_HOME=/cache" in dockerfile
    assert "chown -R 1000:1000 /app" in dockerfile
    assert "chown -R 1000:1000 /cache /tmp" in dockerfile
    assert "chmod -R a+rwX /cache" in dockerfile


def test_dockerfile_keeps_managed_python_out_of_ephemeral_tmp() -> None:
    dockerfile = DOCKERFILE.read_text()

    assert "UV_PYTHON_INSTALL_DIR=/opt/uv-python" in dockerfile
    assert 'python_target="$(readlink -f /app/.venv/bin/python)"' in dockerfile
    assert 'case "${python_target}" in /opt/uv-python/*)' in dockerfile
    assert "/app/.venv/bin/python --version" in dockerfile


def test_compose_uses_balanced_pool_performance_profile() -> None:
    compose = COMPOSE.read_text()

    assert '"127.0.0.1:8191:8191"' in compose
    assert "CAMOUFLARE_API_TOKEN: ${CAMOUFLARE_API_TOKEN:?Set CAMOUFLARE_API_TOKEN}" in compose
    assert "change-me" not in compose
    assert 'POOL_MIN_BROWSERS: "2"' in compose
    assert 'POOL_MAX_BROWSERS: "2"' in compose
    assert 'POOL_MAX_CONTEXTS_PER_BROWSER: "2"' in compose
    assert 'POOL_ACQUIRE_TIMEOUT_MS: "10000"' in compose
    assert 'LOG_FORMAT: "json"' in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "no-new-privileges:true" in compose
    assert 'shm_size: "2gb"' in compose
    assert 'mem_limit: "4g"' in compose
    assert "pids_limit: 512" in compose
    assert "stop_grace_period: 45s" in compose
