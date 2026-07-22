from __future__ import annotations

import importlib

import pytest

import camouflare.config as config_module
from camouflare.config import Settings, normalize_proxy
from camouflare.limits import (
    MAX_COOKIE_BYTES,
    MAX_SESSION_ID_LENGTH,
    MAX_TARGET_HEADER_BYTES,
    MAX_URL_LENGTH,
    json_size,
)
from camouflare.models import DiagnosticsResponse, V1Request, V1Response


def test_v1_request_accepts_flaresolverr_camel_case_fields() -> None:
    req = V1Request.model_validate(
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "maxTimeout": 60000,
            "returnOnlyCookies": True,
            "returnScreenshot": True,
            "waitInSeconds": 2,
            "disableMedia": True,
            "session_ttl_minutes": 5,
            "headers": {"x-ignored": "1"},
            "userAgent": "ignored",
            "download": True,
            "returnRawHtml": True,
        }
    )

    assert req.max_timeout == 60000
    assert req.return_only_cookies is True
    assert req.return_screenshot is True
    assert req.wait_in_seconds == 2
    assert req.disable_media is True
    assert req.session_ttl_minutes == 5
    assert req.headers == {"x-ignored": "1"}


def test_v1_request_keeps_navigation_headers_out_of_extra_headers() -> None:
    req = V1Request.model_validate(
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "headers": {
                "Accept": "text/html",
                "Referer": "https://tickets.example/",
                "User-Agent": "HeaderBrowser/1.0",
                "X-Retry": 1,
            },
            "userAgent": "ContextBrowser/1.0",
        }
    )

    assert req.target_headers() == {
        "Accept": "text/html",
        "X-Retry": "1",
    }
    assert req.target_referer() == "https://tickets.example/"
    assert req.target_user_agent() == "ContextBrowser/1.0"


def test_v1_request_user_agent_header_is_context_only() -> None:
    req = V1Request.model_validate(
        {
            "cmd": "request.get",
            "url": "https://example.com",
            "headers": {"user-agent": "HeaderBrowser/1.0"},
        }
    )

    assert req.target_headers() == {}
    assert req.target_user_agent() == "HeaderBrowser/1.0"


def test_normalize_proxy_extracts_socks5h_url_credentials_for_playwright() -> None:
    proxy = normalize_proxy({"url": "socks5h://user:pass@185.184.26.78:1080"})

    assert proxy == {
        "server": "socks5://185.184.26.78:1080",
        "username": "user",
        "password": "pass",
    }


def test_normalize_proxy_preserves_explicit_credentials_over_url_credentials() -> None:
    proxy = normalize_proxy(
        {
            "url": "http://url-user:url-pass@proxy.example:8080",
            "username": "request-user",
            "password": "request-pass",
        }
    )

    assert proxy == {
        "server": "http://proxy.example:8080",
        "username": "request-user",
        "password": "request-pass",
    }


def test_default_pool_context_concurrency_is_conservative_for_camoufox() -> None:
    assert Settings().pool_max_contexts_per_browser == 1


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("virtual", "virtual"),
    ],
)
def test_settings_parses_headless_env_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
    expected: str | bool,
) -> None:
    monkeypatch.setenv("HEADLESS", raw_value)

    assert Settings().headless == expected


def test_settings_default_headless_is_virtual_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADLESS", raising=False)
    monkeypatch.setattr(config_module.platform, "system", lambda: "Linux")

    assert Settings().headless == "virtual"


def test_settings_default_headless_is_true_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEADLESS", raising=False)
    monkeypatch.setattr(config_module.platform, "system", lambda: "Darwin")

    assert Settings().headless is True


def test_settings_rejects_invalid_headless_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADLESS", "sometimes")

    with pytest.raises(ValueError, match="HEADLESS"):
        Settings()


def test_v1_response_serializes_flaresolverr_camel_case_fields() -> None:
    response = V1Response(
        status="ok",
        message="Challenge not detected!",
        start_timestamp=10,
        end_timestamp=20,
        version="test",
        solution={
            "url": "https://example.com",
            "status": 200,
            "headers": {},
            "response": "<html></html>",
            "cookies": [],
            "user_agent": "FakeBrowser/1.0",
            "turnstile_token": "token",
        },
    )

    assert response.model_dump(by_alias=True)["startTimestamp"] == 10
    assert response.model_dump(by_alias=True)["endTimestamp"] == 20
    assert response.model_dump(by_alias=True)["solution"]["userAgent"] == "FakeBrowser/1.0"
    assert response.model_dump(by_alias=True)["solution"]["turnstile_token"] == "token"


def test_v1_response_serializes_optional_error_resilience_metadata() -> None:
    response = V1Response(
        status="error",
        message="Browser transport closed.",
        error_code="BROWSER_TRANSPORT_CLOSED",  # type: ignore[arg-type]
        retryable=True,
        request_outcome_unknown=False,
        fallback_used=True,
        version="test",
    )

    payload = response.model_dump(by_alias=True, exclude_none=True)

    assert payload["errorCode"] == "BROWSER_TRANSPORT_CLOSED"
    assert payload["retryable"] is True
    assert payload["requestOutcomeUnknown"] is False
    assert payload["fallbackUsed"] is True


def test_v1_response_omits_unused_error_resilience_metadata() -> None:
    payload = V1Response(status="ok", version="test").model_dump(
        by_alias=True,
        exclude_none=True,
    )

    assert "errorCode" not in payload
    assert "retryable" not in payload
    assert "requestOutcomeUnknown" not in payload
    assert "fallbackUsed" not in payload


def test_camouflare_error_carries_machine_readable_metadata() -> None:
    error_module = importlib.import_module("camouflare.errors")
    error = error_module.CamouflareError(
        "Browser transport closed.",
        error_code=error_module.V1ErrorCode.BROWSER_TRANSPORT_CLOSED,
        retryable=True,
    )

    assert str(error) == "Browser transport closed."
    assert error.error_code is error_module.V1ErrorCode.BROWSER_TRANSPORT_CLOSED
    assert error.retryable is True
    assert error.request_outcome_unknown is False
    assert error.solution is None


def test_diagnostics_response_validates_bounded_operational_snapshot() -> None:
    response = DiagnosticsResponse.model_validate(
        {
            "status": "ok",
            "capacity_state": "recovering",
            "pool": {
                "ready_browser_slots": 1,
                "retiring_browser_slots": 1,
                "creating_slots": 1,
                "closing_slots": 0,
                "active_contexts": 2,
                "transient_contexts": 1,
                "persistent_contexts": 1,
                "waiting_requests": 3,
                "usable_context_slots": 0,
                "idle_recyclable_slots": 1,
                "max_browsers": 2,
                "max_contexts_per_browser": 2,
                "max_slots": 4,
            },
            "sessions": {"active": 1, "in_use": 1, "closing": 1, "max_sessions": 32},
            "cleanup": {
                "in_flight": 1,
                "oldest_age_seconds": 0.25,
                "by_kind": {"request": 1},
            },
            "runtime": {
                "playwright_version": "1.61.0",
                "playwright_cancel_patch": "applied",
            },
        }
    )

    assert response.capacity_state == "recovering"
    assert response.pool.max_slots == 4
    assert response.cleanup.by_kind == {"request": 1}


def test_diagnostics_response_rejects_unknown_capacity_state() -> None:
    with pytest.raises(ValueError):
        DiagnosticsResponse.model_validate(
            {
                "capacity_state": "mystery",
                "pool": {
                    "ready_browser_slots": 0,
                    "retiring_browser_slots": 0,
                    "creating_slots": 0,
                    "closing_slots": 0,
                    "active_contexts": 0,
                    "transient_contexts": 0,
                    "persistent_contexts": 0,
                    "waiting_requests": 0,
                    "usable_context_slots": 0,
                    "idle_recyclable_slots": 0,
                    "max_browsers": 1,
                    "max_contexts_per_browser": 1,
                    "max_slots": 1,
                },
                "sessions": {"active": 0, "in_use": 0, "closing": 0, "max_sessions": 1},
                "cleanup": {"in_flight": 0, "by_kind": {}},
                "runtime": {
                    "playwright_version": "unknown",
                    "playwright_cancel_patch": "not-applicable",
                },
            }
        )


def test_v1_request_accepts_structural_limit_boundaries() -> None:
    request = V1Request(
        cmd="request.get",
        url="h" * MAX_URL_LENGTH,
        session="s" * MAX_SESSION_ID_LENGTH,
        headers={f"X-{index}": "v" for index in range(128)},
        cookies=[{"name": f"c{index}", "value": "v"} for index in range(300)],
    )

    assert len(request.url or "") == MAX_URL_LENGTH
    assert len(request.session or "") == MAX_SESSION_ID_LENGTH


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "h" * (MAX_URL_LENGTH + 1)},
        {"session": "s" * (MAX_SESSION_ID_LENGTH + 1)},
        {"headers": {f"X-{index}": "v" for index in range(129)}},
        {"headers": {"X-Large": "v" * 65_536}},
        {"cookies": [{"name": f"c{index}", "value": "v"} for index in range(301)]},
        {"cookies": [{"name": "large", "value": "v" * 262_144}]},
    ],
)
def test_v1_request_rejects_structural_limit_plus_one(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        V1Request(cmd="request.get", **payload)


def test_header_and_cookie_byte_limits_accept_exact_boundary() -> None:
    header_overhead = json_size({"X": ""})
    cookie_overhead = json_size([{"name": "x", "value": ""}])

    request = V1Request(
        cmd="request.get",
        headers={"X": "v" * (MAX_TARGET_HEADER_BYTES - header_overhead)},
        cookies=[{"name": "x", "value": "v" * (MAX_COOKIE_BYTES - cookie_overhead)}],
    )

    assert json_size(request.headers) == MAX_TARGET_HEADER_BYTES
    assert json_size(request.cookies) == MAX_COOKIE_BYTES


def test_header_and_cookie_byte_limits_reject_boundary_plus_one() -> None:
    header_overhead = json_size({"X": ""})
    cookie_overhead = json_size([{"name": "x", "value": ""}])

    with pytest.raises(ValueError, match="headers"):
        V1Request(
            cmd="request.get",
            headers={"X": "v" * (MAX_TARGET_HEADER_BYTES - header_overhead + 1)},
        )
    with pytest.raises(ValueError, match="cookies"):
        V1Request(
            cmd="request.get",
            cookies=[
                {
                    "name": "x",
                    "value": "v" * (MAX_COOKIE_BYTES - cookie_overhead + 1),
                }
            ],
        )
