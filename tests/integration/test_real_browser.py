from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
from html import unescape

import pytest
from fastapi import FastAPI
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
    browser_app: FastAPI,
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

    # The hard response deadline intentionally returns before cancellation
    # unwind. Wait for that independently owned cleanup before using the same
    # single-browser fixture for a separate protocol-cancellation probe.
    cleanup_deadline = time.monotonic() + 5
    while browser_app.state.cleanup.snapshot().in_flight > 0:
        assert time.monotonic() < cleanup_deadline
        await asyncio.sleep(0.01)
    assert browser_app.state.pool.snapshot().active_contexts == 0

    # Cancel a real Playwright protocol navigation while the fixture deliberately
    # withholds its response. This exercises the guarded _inner_send workaround,
    # rather than the direct-HTTP-first or post-navigation wait paths.
    pool = browser_app.state.pool
    async with pool.lease_context(no_viewport=True) as lease:
        page = await asyncio.wait_for(lease.context.new_page(), timeout=10)
        try:
            navigation = asyncio.create_task(
                page.goto(
                    f"{local_http_server.base_url}/slow?seconds=2",
                    wait_until="domcontentloaded",
                    timeout=10_000,
                )
            )
            done, _ = await asyncio.wait({navigation}, timeout=0.1)
            assert not done
            navigation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await navigation
        finally:
            await page.close()

    diagnostics = await browser_client.get("/diagnostics")
    assert diagnostics.status_code == 200
    assert diagnostics.json()["runtime"]["playwright_cancel_patch"] == "applied"

    # Exercise recovery immediately after the cancelled navigation. The short
    # test-only max age makes the current idle slot stale; /ready must retire it,
    # launch a replacement, and release its probe context.
    original_max_age = pool._browser_max_age_seconds
    pool._browser_max_age_seconds = 0.05
    try:
        await asyncio.sleep(0.06)
        ready = await browser_client.get("/ready")
    finally:
        pool._browser_max_age_seconds = original_max_age

    assert ready.status_code == 200, ready.json()
    recovered = _solution(
        await _post_command(
            browser_client,
            {
                "cmd": "request.get",
                "url": f"{local_http_server.base_url}/get",
                "maxTimeout": 15_000,
            },
        )
    )
    assert recovered["status"] == 200
    snapshot = pool.snapshot()
    assert snapshot.active_contexts == 0
    assert snapshot.usable_context_slots > 0
