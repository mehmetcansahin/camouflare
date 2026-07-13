from __future__ import annotations

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
