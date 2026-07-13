from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from html import unescape

import pytest
from httpx import AsyncClient, Response

from tests.integration.support import LocalHttpServer


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


pytestmark = [
    pytest.mark.integration,
    pytest.mark.browser,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not _enabled("CAMOUFLARE_RUN_BROWSER_TESTS"),
        reason="set CAMOUFLARE_RUN_BROWSER_TESTS=1 to launch the real Camoufox browser",
    ),
]


async def _post_command(client: AsyncClient, payload: dict[str, object]) -> Response:
    return await client.post("/v1", json=payload)


def _solution(response: Response) -> dict[str, object]:
    body = response.json()
    assert response.status_code == 200, body
    assert body["status"] == "ok", body
    return body["solution"]


def _element_text(source: str, element_id: str) -> str:
    match = re.search(
        rf'<(?:pre|main)[^>]*id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</(?:pre|main)>',
        source,
        flags=re.DOTALL,
    )
    assert match is not None, f"element #{element_id} was not present in {source!r}"
    return unescape(match.group(1))


async def test_get_redirect_delayed_javascript_and_screenshot(
    browser_client: AsyncClient,
    local_http_server: LocalHttpServer,
) -> None:
    base_url = local_http_server.base_url

    get_solution = _solution(
        await _post_command(
            browser_client,
            {"cmd": "request.get", "url": f"{base_url}/get", "maxTimeout": 15_000},
        )
    )
    assert get_solution["status"] == 200
    assert 'id="get-result">get-ok' in str(get_solution["response"])

    redirect_solution = _solution(
        await _post_command(
            browser_client,
            {"cmd": "request.get", "url": f"{base_url}/redirect", "maxTimeout": 15_000},
        )
    )
    assert redirect_solution["url"] == f"{base_url}/final?from=redirect"
    assert 'id="redirect-result">redirect-ok' in str(redirect_solution["response"])

    delayed_solution = _solution(
        await _post_command(
            browser_client,
            {
                "cmd": "request.get",
                "url": f"{base_url}/delayed",
                "maxTimeout": 15_000,
                "waitInSeconds": 1,
            },
        )
    )
    assert 'id="delayed-result">javascript-ready' in str(delayed_solution["response"])

    screenshot_solution = _solution(
        await _post_command(
            browser_client,
            {
                "cmd": "request.get",
                "url": f"{base_url}/screenshot",
                "maxTimeout": 15_000,
                "returnScreenshot": True,
            },
        )
    )
    screenshot = screenshot_solution.get("screenshot")
    assert isinstance(screenshot, str) and screenshot
    assert base64.b64decode(screenshot, validate=True).startswith(b"\x89PNG\r\n\x1a\n")


async def test_form_and_json_post_preserve_the_request_body(
    browser_client: AsyncClient,
    local_http_server: LocalHttpServer,
) -> None:
    base_url = local_http_server.base_url

    form_solution = _solution(
        await _post_command(
            browser_client,
            {
                "cmd": "request.post",
                "url": f"{base_url}/post/form",
                "postData": "color=blue&tag=one&tag=two&empty=",
                "maxTimeout": 15_000,
            },
        )
    )
    form_values = json.loads(_element_text(str(form_solution["response"]), "form-values"))
    assert form_values == {"color": ["blue"], "empty": [""], "tag": ["one", "two"]}

    post_data = '{"alpha":1,"nested":{"ok":true}}'
    json_solution = _solution(
        await _post_command(
            browser_client,
            {
                "cmd": "request.post",
                "url": f"{base_url}/post/json",
                "postData": post_data,
                "headers": {"Content-Type": "application/json"},
                "maxTimeout": 15_000,
            },
        )
    )
    source = str(json_solution["response"])
    assert f'data-body-sha256="{hashlib.sha256(post_data.encode()).hexdigest()}"' in source
    assert 'data-content-type="application/json"' in source
    assert _element_text(source, "json-body") == post_data


async def test_persistent_sessions_keep_cookies_isolated(
    browser_client: AsyncClient,
    local_http_server: LocalHttpServer,
) -> None:
    base_url = local_http_server.base_url
    created: list[str] = []
    try:
        for session_id in ("integration-alpha", "integration-beta"):
            response = await _post_command(
                browser_client,
                {"cmd": "sessions.create", "session": session_id, "maxTimeout": 15_000},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "ok"
            created.append(session_id)

        set_solution = _solution(
            await _post_command(
                browser_client,
                {
                    "cmd": "request.get",
                    "url": f"{base_url}/cookies/set?value=alpha",
                    "session": "integration-alpha",
                    "maxTimeout": 15_000,
                },
            )
        )
        assert any(
            cookie.get("name") == "camouflare_fixture" and cookie.get("value") == "alpha"
            for cookie in set_solution["cookies"]
        )

        alpha_solution = _solution(
            await _post_command(
                browser_client,
                {
                    "cmd": "request.get",
                    "url": f"{base_url}/cookies/read",
                    "session": "integration-alpha",
                    "maxTimeout": 15_000,
                },
            )
        )
        beta_solution = _solution(
            await _post_command(
                browser_client,
                {
                    "cmd": "request.get",
                    "url": f"{base_url}/cookies/read",
                    "session": "integration-beta",
                    "maxTimeout": 15_000,
                },
            )
        )
        assert "camouflare_fixture=alpha" in _element_text(
            str(alpha_solution["response"]), "cookie-header"
        )
        assert "camouflare_fixture" not in _element_text(
            str(beta_solution["response"]), "cookie-header"
        )
    finally:
        for session_id in created:
            await _post_command(
                browser_client,
                {"cmd": "sessions.destroy", "session": session_id, "maxTimeout": 15_000},
            )


async def test_command_timeout_cancels_requested_wait(
    browser_client: AsyncClient,
    local_http_server: LocalHttpServer,
) -> None:
    started = time.monotonic()
    response = await _post_command(
        browser_client,
        {
            "cmd": "request.get",
            "url": f"{local_http_server.base_url}/get",
            "maxTimeout": 250,
            "waitInSeconds": 2,
        },
    )
    elapsed = time.monotonic() - started

    body = response.json()
    assert response.status_code == 500, body
    assert body["status"] == "error"
    assert "timeout" in body["message"].lower()
    assert elapsed < 5
