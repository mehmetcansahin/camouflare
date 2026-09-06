from __future__ import annotations

import os
import subprocess
import sys
from typing import Any

import pytest

import camouflare.__main__ as cli
from camouflare import __version__


def test_cli_version_uses_package_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["camouflare", "--version"])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 0
    assert capsys.readouterr().out.strip() == f"camouflare {__version__}"


def test_cli_configures_logging_and_starts_exactly_one_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["camouflare"])
    app = object()
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(
        cli,
        "configure_logging",
        lambda **kwargs: calls.append(("logging", kwargs)),
    )
    monkeypatch.setattr(cli, "create_app", lambda **kwargs: calls.append(("app", kwargs)) or app)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda target, **kwargs: calls.append(("run", (target, kwargs))),
    )

    cli.main()

    assert [name for name, _ in calls] == ["logging", "app", "run"]
    run_target, run_options = calls[-1][1]
    assert run_target is app
    assert run_options["log_config"] is None


def test_external_uvicorn_entry_point_refuses_tokenless_start() -> None:
    environment = os.environ.copy()
    environment.pop("CAMOUFLARE_API_TOKEN", None)
    environment.update({"HOST": "127.0.0.1", "POOL_MIN_BROWSERS": "0"})

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "camouflare.asgi:app",
            "--host",
            "0.0.0.0",
            "--port",
            "0",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "CAMOUFLARE_API_TOKEN is required" in completed.stdout + completed.stderr


def test_external_asgi_entry_point_loads_with_token() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "CAMOUFLARE_API_TOKEN": "test-only-token",
            "HOST": "127.0.0.1",
            "POOL_MIN_BROWSERS": "0",
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", "import camouflare.asgi"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
