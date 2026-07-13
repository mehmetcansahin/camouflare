from __future__ import annotations

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
from camouflare.models import V1Request, V1Response


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
