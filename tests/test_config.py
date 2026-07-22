from __future__ import annotations

import pytest

from camouflare import __version__
from camouflare.config import Settings


def test_settings_reads_env_at_instantiation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Env set after import must be reflected, matching headless's default_factory.
    monkeypatch.setenv("POOL_MAX_BROWSERS", "4")
    monkeypatch.setenv("MAX_SESSIONS", "7")
    monkeypatch.setenv("CLEANUP_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("READINESS_TIMEOUT_MS", "2500")
    monkeypatch.setenv("CAMOUFLARE_API_TOKEN", "secret-token")

    settings = Settings()

    assert settings.pool_max_browsers == 4
    assert settings.max_sessions == 7
    assert settings.cleanup_timeout_seconds == 12
    assert settings.readiness_timeout_ms == 2500
    assert settings.readiness_timeout_seconds == 2.5
    assert settings.camouflare_api_token == "secret-token"


def test_version_uses_the_single_package_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERSION", "9.9.9")

    assert Settings().version == __version__ == "1.3.0"


def test_settings_ignores_non_integer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "not-a-number")

    assert Settings().port == 8191


def test_cleanup_and_readiness_deadline_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLEANUP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("READINESS_TIMEOUT_MS", raising=False)

    settings = Settings()

    assert settings.cleanup_timeout_seconds == 10
    assert settings.readiness_timeout_ms == 15_000
    assert settings.readiness_timeout_seconds == 15


def test_challenge_solver_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHALLENGE_SOLVER", raising=False)

    assert Settings().challenge_solver == "none"


def test_challenge_solver_accepts_click(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALLENGE_SOLVER", "Click")

    assert Settings().challenge_solver == "click"


def test_challenge_solver_accepts_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALLENGE_SOLVER", "None")

    assert Settings().challenge_solver == "none"


def test_challenge_solver_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHALLENGE_SOLVER", "banana")

    with pytest.raises(ValueError, match="CHALLENGE_SOLVER"):
        Settings()


def test_settings_defaults_to_loopback_without_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("CAMOUFLARE_API_TOKEN", raising=False)

    assert Settings().host == "127.0.0.1"


def test_settings_require_api_token_for_non_loopback_host() -> None:
    with pytest.raises(ValueError, match="CAMOUFLARE_API_TOKEN"):
        Settings(host="0.0.0.0", camouflare_api_token=None)

    settings = Settings(host="0.0.0.0", camouflare_api_token="secret-token")
    assert settings.host == "0.0.0.0"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"port": 0}, "PORT"),
        ({"pool_min_browsers": -1}, "POOL_MIN_BROWSERS"),
        ({"pool_max_browsers": 0}, "POOL_MAX_BROWSERS"),
        (
            {"pool_min_browsers": 3, "pool_max_browsers": 2},
            "POOL_MIN_BROWSERS",
        ),
        ({"pool_max_contexts_per_browser": 0}, "POOL_MAX_CONTEXTS_PER_BROWSER"),
        ({"pool_reserved_transient_contexts": -1}, "POOL_RESERVED_TRANSIENT_CONTEXTS"),
        ({"pool_acquire_timeout_ms": 0}, "POOL_ACQUIRE_TIMEOUT_MS"),
        ({"max_sessions": 0}, "MAX_SESSIONS"),
        ({"session_ttl_minutes": 0}, "SESSION_TTL_MINUTES"),
        ({"browser_max_uses": 0}, "BROWSER_MAX_USES"),
        ({"browser_max_age_minutes": 0}, "BROWSER_MAX_AGE_MINUTES"),
        ({"max_request_body_bytes": 0}, "MAX_REQUEST_BODY_BYTES"),
        ({"max_response_body_bytes": 0}, "MAX_RESPONSE_BODY_BYTES"),
        ({"max_screenshot_bytes": 0}, "MAX_SCREENSHOT_BYTES"),
        ({"max_solution_bytes": 0}, "MAX_SOLUTION_BYTES"),
        ({"max_timeout_ms": 0}, "MAX_TIMEOUT_MS"),
        ({"max_session_ttl_minutes": 0}, "MAX_SESSION_TTL_MINUTES"),
        ({"session_reaper_interval_seconds": 0}, "SESSION_REAPER_INTERVAL_SECONDS"),
        ({"shutdown_timeout_seconds": 0}, "SHUTDOWN_TIMEOUT_SECONDS"),
        ({"cleanup_timeout_seconds": 0}, "CLEANUP_TIMEOUT_SECONDS"),
        ({"readiness_timeout_ms": 0}, "READINESS_TIMEOUT_MS"),
    ],
)
def test_settings_reject_invalid_numeric_configuration(
    overrides: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(**overrides)


def test_settings_expose_resource_limits() -> None:
    settings = Settings(
        max_response_body_bytes=123,
        max_screenshot_bytes=456,
        max_solution_bytes=789,
    )

    assert settings.resource_limits.response_body_bytes == 123
    assert settings.resource_limits.screenshot_bytes == 456
    assert settings.resource_limits.solution_bytes == 789


def test_settings_validate_ttl_ceiling_and_log_format() -> None:
    with pytest.raises(ValueError, match="MAX_SESSION_TTL_MINUTES"):
        Settings(session_ttl_minutes=61, max_session_ttl_minutes=60)

    with pytest.raises(ValueError, match="LOG_FORMAT"):
        Settings(log_format="xml")
