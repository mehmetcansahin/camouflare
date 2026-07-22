from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import pytest

import camouflare.metrics as metrics_module
import camouflare.solver as solver_module
from camouflare.errors import V1ErrorCode
from camouflare.limits import MAX_COOKIE_BYTES, ResourceLimitError, ResourceLimits, json_size
from camouflare.models import V1Request
from camouflare.solver import MEDIA_PATTERNS, _submit_post_form, solve_request
from camouflare.timer import TimeoutTimer
from tests.fakes import FakeContext, FakePage, FakeResponse


def _metric_sample_value(collector: object, sample_name: str, **labels: str) -> float:
    for family in collector.collect():  # type: ignore[attr-defined]
        for sample in family.samples:
            if sample.name == sample_name and sample.labels == labels:
                return float(sample.value)
    return 0.0


@pytest.mark.anyio
async def test_networkidle_timeout_does_not_prevent_result_collection() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.wait_failures.add("networkidle")

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert page.load_states == ["networkidle"]
    assert result.solution is not None
    assert result.solution.url == "https://example.com"
    assert result.fallback_used is None


@pytest.mark.anyio
async def test_wait_in_seconds_happens_before_cookie_collection() -> None:
    events: list[str] = []

    class EventContext(FakeContext):
        async def cookies(self):  # type: ignore[no-untyped-def]
            events.append("cookies")
            return await super().cookies()

    async def wait(seconds: float) -> None:
        events.append(f"wait:{seconds}")

    context = EventContext()
    page = await context.new_page()

    await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            maxTimeout=60000,
            waitInSeconds=3,
        ),
        context=context,
        page=page,
        sleep=wait,
    )

    assert events == ["wait:3", "cookies"]


@pytest.mark.anyio
async def test_wait_in_seconds_is_bounded_by_request_timeout() -> None:
    async def slow_wait(_: float) -> None:
        await asyncio.sleep(1)

    context = FakeContext()
    page = await context.new_page()

    result = await asyncio.wait_for(
        solve_request(
            V1Request(
                cmd="request.get",
                url="https://example.com",
                maxTimeout=20,
                waitInSeconds=60,
            ),
            context=context,
            page=page,
            sleep=slow_wait,
        ),
        timeout=5.0,
    )

    assert result.status == "error"
    assert "timed out" in result.message.lower()
    assert result.solution is not None


@pytest.mark.anyio
async def test_return_only_cookies_strips_response_headers_and_screenshot() -> None:
    context = FakeContext()
    page = await context.new_page()

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            maxTimeout=60000,
            returnOnlyCookies=True,
            returnScreenshot=True,
        ),
        context=context,
        page=page,
    )

    assert result.solution is not None
    assert result.solution.cookies
    assert result.solution.response is None
    assert result.solution.headers is None
    assert result.solution.screenshot is None


@pytest.mark.anyio
async def test_disable_media_installs_context_routes() -> None:
    context = FakeContext()
    page = await context.new_page()

    await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            maxTimeout=60000,
            disableMedia=True,
        ),
        context=context,
        page=page,
    )

    assert context.routes
    assert any("png" in pattern for pattern, _ in context.routes)


@pytest.mark.anyio
async def test_disable_media_routes_installed_once_per_reused_context() -> None:
    context = FakeContext()

    for _ in range(3):
        page = await context.new_page()
        await solve_request(
            V1Request(
                cmd="request.get",
                url="https://example.com",
                maxTimeout=60000,
                disableMedia=True,
            ),
            context=context,
            page=page,
        )

    assert len(context.routes) == len(MEDIA_PATTERNS)


@pytest.mark.anyio
async def test_request_headers_are_applied_to_page_before_navigation() -> None:
    context = FakeContext()
    page = await context.new_page()

    await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            maxTimeout=60000,
            headers={
                "Accept": "text/html",
                "Referer": "https://tickets.example/",
                "X-Retry": 1,
            },
        ),
        context=context,
        page=page,
    )

    assert page.events[:2] == ["headers", "goto"]
    assert page.extra_http_headers_calls == [
        {
            "Accept": "text/html",
            "X-Retry": "1",
        }
    ]
    assert page.goto_calls[-1]["referer"] == "https://tickets.example/"


@pytest.mark.anyio
async def test_user_agent_header_is_applied_to_page_before_navigation() -> None:
    context = FakeContext(options={"user_agent": "ContextBrowser/1.0"})
    page = await context.new_page()

    await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            maxTimeout=60000,
            headers={"User-Agent": "ContextBrowser/1.0"},
        ),
        context=context,
        page=page,
    )

    assert page.events[:2] == ["headers", "goto"]
    assert page.extra_http_headers_calls == [{"User-Agent": "ContextBrowser/1.0"}]
    assert page.goto_calls[-1]["referer"] is None


@pytest.mark.anyio
async def test_user_agent_header_overrides_request_header_and_navigator() -> None:
    context = FakeContext(options={"user_agent": "CamouflareHeaderTest/1.0"})
    page = await context.new_page()

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://httpbingo.org/headers",
            maxTimeout=60000,
            headers={"User-Agent": "CamouflareHeaderTest/1.0"},
        ),
        context=context,
        page=page,
    )

    assert page.extra_http_headers_calls == [{"User-Agent": "CamouflareHeaderTest/1.0"}]
    assert page.init_scripts
    assert "setNavigatorUserAgent" in page.init_scripts[-1]
    assert "CamouflareHeaderTest/1.0" in page.init_scripts[-1]
    assert result.solution is not None
    assert result.solution.user_agent == "CamouflareHeaderTest/1.0"


@pytest.mark.anyio
async def test_request_post_submits_form_encoded_post_data() -> None:
    context = FakeContext()
    page = await context.new_page()

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://example.com/form",
            postData="a=b&c=d",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert page.posted_form == {"a": "b", "c": "d"}
    assert page.goto_calls[-1]["url"] == "https://example.com/form"


@pytest.mark.anyio
async def test_request_post_uses_context_request_for_form_endpoint_body() -> None:
    class ApiRequest:
        def __init__(self) -> None:
            self.post_calls: list[dict[str, Any]] = []
            self.response: FakeResponse | None = None

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.post_calls.append({"url": url, **kwargs})
            self.response = FakeResponse(
                status=200,
                headers={"content-type": "text/html; charset=UTF-8"},
                text_value='<div class="col-md-3 cur"><a class="f18">Başlama</a></div>',
            )
            self.response.disposed = False  # type: ignore[attr-defined]

            async def dispose() -> None:
                assert self.response is not None
                self.response.disposed = True  # type: ignore[attr-defined]

            self.response.dispose = dispose  # type: ignore[attr-defined]
            return self.response

    context = FakeContext()
    context.request = ApiRequest()  # type: ignore[attr-defined]
    page = await context.new_page()

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://festivall.example/calendar-ic.php",
            postData="starting=1&pageno=1",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert "Başlama" in result.solution.response
    assert context.request.post_calls == [  # type: ignore[attr-defined]
        {
            "url": "https://festivall.example/calendar-ic.php",
            "data": "starting=1&pageno=1",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "timeout": 60000,
        }
    ]
    assert page.posted_form is None
    assert context.request.response is not None  # type: ignore[attr-defined]
    assert context.request.response.disposed is True  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_request_post_sends_json_content_type_as_raw_body() -> None:
    class ApiRequest:
        def __init__(self) -> None:
            self.post_calls: list[dict[str, Any]] = []

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.post_calls.append({"url": url, **kwargs})
            return FakeResponse(
                status=200,
                headers={"content-type": "application/json; charset=utf-8"},
                text_value='{"data":{"hits":[{"id":1}]}}',
            )

    context = FakeContext()
    context.request = ApiRequest()  # type: ignore[attr-defined]
    page = await context.new_page()
    post_data = '{"search":"","cityNames":["İstanbul"],"offset":0,"limit":100}'

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://api.example/searchEvent",
            postData=post_data,
            headers={"Content-Type": "application/json-patch+json"},
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == '{"data":{"hits":[{"id":1}]}}'
    assert context.request.post_calls == [  # type: ignore[attr-defined]
        {
            "url": "https://api.example/searchEvent",
            "data": post_data,
            "headers": {"Content-Type": "application/json-patch+json"},
            "timeout": 60000,
        }
    ]
    assert page.posted_form is None


@pytest.mark.anyio
async def test_request_post_uses_browser_navigation_with_exact_json_body() -> None:
    continued: dict[str, Any] = {}

    class RouteRequest:
        async def all_headers(self) -> dict[str, str]:
            return {"Accept": "text/html", "Content-Length": "0"}

    class Route:
        request = RouteRequest()

        async def continue_(self, **kwargs: Any) -> None:
            continued.update(kwargs)

    class RoutedPage(FakePage):
        def __init__(self, context: FakeContext) -> None:
            super().__init__(context)
            self.route_url = ""
            self.route_times = 0
            self.route_handler: Any | None = None

        async def route(self, url: str, handler: Any, *, times: int) -> None:
            self.route_url = url
            self.route_times = times
            self.route_handler = handler

        async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
            assert self.route_handler is not None
            await self.route_handler(Route())
            self.url = url
            self.goto_calls.append({"url": url, **kwargs})
            return FakeResponse(
                status=200,
                headers={"content-type": "application/json"},
                text_value='{"ok":true}',
            )

    context = FakeContext()
    page = RoutedPage(context)
    post_data = '{"search":"İstanbul"}'

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://api.example/search#results",
            postData=post_data,
            headers={"Content-Type": "application/json", "X-Request-ID": "abc"},
            returnScreenshot=True,
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == '{"ok":true}'
    assert result.solution.screenshot is not None
    assert page.route_url == "https://api.example/search"
    assert page.route_times == 1
    assert page.goto_calls[0]["url"] == "https://api.example/search#results"
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert continued["method"] == "POST"
    assert continued["post_data"] == post_data
    assert continued["headers"]["Content-Type"] == "application/json"
    assert continued["headers"]["X-Request-ID"] == "abc"
    assert all(name.lower() != "content-length" for name in continued["headers"])


@pytest.mark.anyio
async def test_json_post_challenge_fallback_does_not_reencode_body_as_form() -> None:
    class ChallengeApiRequest:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.calls.append({"url": url, **kwargs})
            return FakeResponse(
                status=403,
                headers={"content-type": "text/html"},
                text_value="<html><title>Just a moment...</title></html>",
            )

    context = FakeContext()
    context.request = ChallengeApiRequest()  # type: ignore[attr-defined]
    page = await context.new_page()
    post_data = '{"query":"tickets"}'

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://api.example/search",
            postData=post_data,
            headers={"Content-Type": "application/json"},
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "error"
    assert "Challenge remained" in result.message
    assert page.posted_form is None
    assert context.request.calls[0]["data"] == post_data  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_request_post_hidden_form_preserves_decoded_values() -> None:
    class HtmlFormPage:
        def __init__(self) -> None:
            self.html = ""
            self.evaluated: list[str] = []
            self.load_states: list[str] = []

        async def set_content(self, html: str) -> None:
            self.html = html

        async def evaluate(self, script: str) -> str:
            self.evaluated.append(script)
            return ""

        async def wait_for_load_state(
            self,
            state: str = "load",
            *,
            timeout: float | None = None,
        ) -> None:
            self.load_states.append(state)

    page = HtmlFormPage()

    await _submit_post_form(
        page,
        V1Request(
            cmd="request.post",
            url="https://example.com/form",
            postData="q=hello+world&literal=a%25b",
        ),
        TimeoutTimer(60000),
    )

    assert 'name="q"' in page.html
    assert 'value="hello world"' in page.html
    assert 'name="literal"' in page.html
    assert 'value="a%b"' in page.html
    assert "hello%20world" not in page.html
    assert "<script>" not in page.html
    assert "camouflare-post-form" in page.evaluated[-1]


@pytest.mark.anyio
async def test_request_post_hidden_form_returns_navigation_response_when_available() -> None:
    class NavigationInfo:
        def __init__(self, response: FakeResponse) -> None:
            self.value = response

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class HtmlFormPage:
        def __init__(self) -> None:
            self.html = ""
            self.evaluated: list[str] = []
            self.expect_navigation_calls: list[dict[str, object]] = []
            self.navigation_response = FakeResponse(status=201, headers={"x-post": "ok"})

        async def set_content(self, html: str) -> None:
            self.html = html

        async def evaluate(self, script: str) -> str:
            self.evaluated.append(script)
            return ""

        def expect_navigation(self, **kwargs: object) -> NavigationInfo:
            self.expect_navigation_calls.append(kwargs)
            return NavigationInfo(self.navigation_response)

    page = HtmlFormPage()

    response = await _submit_post_form(
        page,
        V1Request(
            cmd="request.post",
            url="https://example.com/form",
            postData="a=b",
        ),
        TimeoutTimer(60000),
    )

    assert response is page.navigation_response
    assert page.expect_navigation_calls == [{"timeout": 60000, "wait_until": "domcontentloaded"}]
    assert page.evaluated


@pytest.mark.anyio
async def test_get_navigation_waits_for_existing_commit_without_retrying_request() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com/slow", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert [call["wait_until"] for call in page.goto_calls] == ["domcontentloaded"]
    assert page.load_states == ["commit", "networkidle"]
    assert page.goto_calls[0]["timeout"] < 60000


@pytest.mark.anyio
async def test_xml_response_returns_raw_response_text_not_browser_viewer_html() -> None:
    raw_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.passo.com.tr/event/example</loc></url>"
        "</urlset>"
    )
    context = FakeContext()
    page = await context.new_page()
    page.goto_response = FakeResponse(
        status=200,
        headers={"content-type": "text/xml; charset=utf-8"},
        text_value=raw_xml,
    )
    page.content_value = (
        '<html><body><table id="sitemap">'
        "<tr><td>https://www.passo.com.tr/event/example</td></tr>"
        "</table></body></html>"
    )

    result = await solve_request(
        V1Request(cmd="request.get", url="https://www.passo.com.tr/sitemap.xml"),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == raw_xml
    assert "<urlset" in result.solution.response
    assert 'id="sitemap"' not in result.solution.response


@pytest.mark.anyio
async def test_navigation_error_returns_partial_solution() -> None:
    context = FakeContext(options={"user_agent": "DebugBrowser/1.0"})
    page = await context.new_page()
    page.url = "https://example.com/challenge"
    page.content_value = "<html><title>Partial</title><body>timeout</body></html>"
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")
    page.wait_failures.add("commit")

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com/challenge",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "error"
    assert "Navigation failed" in result.message
    assert "https://example.com/challenge" in result.message
    assert result.solution is not None
    assert result.solution.url == "https://example.com/challenge"
    assert result.solution.response == page.content_value
    assert result.solution.user_agent == "DebugBrowser/1.0"
    assert result.error_code is V1ErrorCode.NAVIGATION_TIMEOUT
    assert result.retryable is True
    assert result.request_outcome_unknown is False


@pytest.mark.anyio
async def test_post_transport_failure_reports_uncertain_non_retryable_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = RuntimeError(
        "Page.goto: Connection closed while reading from the driver"
    )

    metric_before = _metric_sample_value(
        metrics_module.BROWSER_TRANSPORT_ERROR_COUNTER,
        "camouflare_browser_transport_error_total",
        phase="navigation",
    )
    with caplog.at_level(logging.WARNING, logger="camouflare.solver"):
        result = await solve_request(
            V1Request(
                cmd="request.post",
                url="https://example.com/orders",
                postData="item=1",
                maxTimeout=60000,
            ),
            context=context,
            page=page,
        )

    assert result.status == "error"
    assert result.error_code is V1ErrorCode.BROWSER_TRANSPORT_CLOSED
    assert result.retryable is False
    assert result.request_outcome_unknown is True
    assert len(page.goto_calls) == 1
    assert (
        _metric_sample_value(
            metrics_module.BROWSER_TRANSPORT_ERROR_COUNTER,
            "camouflare_browser_transport_error_total",
            phase="navigation",
        )
        == metric_before + 1
    )
    event = next(
        record for record in caplog.records if record.message == "Browser transport error."
    )
    assert event.phase == "navigation"  # type: ignore[attr-defined]
    assert event.fallback_used is False  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_commit_timeout_uses_direct_http_fallback_for_ajax_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        nonlocal direct_calls
        direct_calls += 1
        assert url == "https://example.com/search?ajax=true"
        assert request.cmd == "request.get"
        assert timer.remaining_ms > 0
        response = FakeResponse(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value='<div class="product">ok</div>',
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")
    page.wait_failures.add("commit")

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com/search?ajax=true",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
        allow_direct_http_first=False,
    )

    assert direct_calls == 1
    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.status == 200
    assert result.solution.response == '<div class="product">ok</div>'
    assert result.fallback_used is True


@pytest.mark.anyio
async def test_commit_timeout_direct_http_challenge_keeps_navigation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        nonlocal direct_calls
        direct_calls += 1
        response = FakeResponse(
            status=403,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value="<html><title>Just a moment...</title></html>",
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")
    page.wait_failures.add("commit")

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com/search?ajax=true",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
        allow_direct_http_first=False,
    )

    assert direct_calls == 1
    assert result.status == "error"
    assert "Navigation failed" in result.message


@pytest.mark.anyio
async def test_commit_timeout_does_not_use_direct_http_when_fallback_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        nonlocal direct_calls
        direct_calls += 1
        return FakeResponse()

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")
    page.wait_failures.add("commit")

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com/search?ajax=true",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
        allow_direct_http_fallback=False,
        allow_direct_http_first=False,
    )

    assert direct_calls == 0
    assert result.status == "error"


@pytest.mark.anyio
async def test_get_transport_crash_uses_direct_http_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        assert url == "https://biletino.example/search?ajax=true"
        assert request.cmd == "request.get"
        assert timer.remaining_ms > 0
        response = FakeResponse(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value='<div class="col-md-4 product"><a class="card-image event-url"></a></div>',
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = RuntimeError(
        "Page.goto: Connection closed while reading from the driver"
    )

    metric_before = _metric_sample_value(
        metrics_module.BROWSER_TRANSPORT_ERROR_COUNTER,
        "camouflare_browser_transport_error_total",
        phase="navigation",
    )
    with caplog.at_level(logging.INFO, logger="camouflare.navigation"):
        result = await solve_request(
            V1Request(
                cmd="request.get",
                url="https://biletino.example/search?ajax=true",
                maxTimeout=60000,
            ),
            context=context,
            page=page,
            allow_direct_http_first=False,
        )

    assert result.status == "ok"
    assert result.message == "Challenge not detected!"
    assert result.solution is not None
    assert result.solution.status == 200
    assert "event-url" in result.solution.response
    assert result.fallback_used is True
    assert (
        _metric_sample_value(
            metrics_module.BROWSER_TRANSPORT_ERROR_COUNTER,
            "camouflare_browser_transport_error_total",
            phase="navigation",
        )
        == metric_before + 1
    )
    event = next(
        record for record in caplog.records if record.message == "Browser transport error."
    )
    assert event.phase == "navigation"  # type: ignore[attr-defined]
    assert event.error_type == "RuntimeError"  # type: ignore[attr-defined]
    assert event.browser_state is None  # type: ignore[attr-defined]
    assert event.slot_uses is None  # type: ignore[attr-defined]
    assert event.slot_active_contexts is None  # type: ignore[attr-defined]
    assert event.retire_reason is None  # type: ignore[attr-defined]
    assert event.fallback_used is True  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_get_transport_fallback_preserves_provenance_on_challenge_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        response = FakeResponse(
            status=403,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value="<html><title>Just a moment...</title></html>",
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = RuntimeError(
        "Page.goto: Connection closed while reading from the driver"
    )

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        allow_direct_http_first=False,
    )

    assert result.status == "error"
    assert result.error_code is V1ErrorCode.CHALLENGE_FAILED
    assert result.fallback_used is True


@pytest.mark.anyio
async def test_failed_get_transport_fallback_preserves_browser_error_observability(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        raise RuntimeError("direct HTTP failed")

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.goto_failures["domcontentloaded"] = RuntimeError(
        "Page.goto: Connection closed while reading from the driver"
    )

    with caplog.at_level(logging.WARNING, logger="camouflare.navigation"):
        result = await solve_request(
            V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
            context=context,
            page=page,
            allow_direct_http_first=False,
        )

    assert result.status == "error"
    assert result.error_code is V1ErrorCode.BROWSER_TRANSPORT_CLOSED
    assert result.fallback_used is None
    event = next(
        record for record in caplog.records if record.message == "Browser transport error."
    )
    assert event.phase == "navigation"  # type: ignore[attr-defined]
    assert event.fallback_used is False  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_ajax_get_uses_direct_http_before_browser_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        assert "ajax=true" in url
        assert request.cmd == "request.get"
        assert timer.remaining_ms > 0
        response = FakeResponse(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value='<div class="col-md-4 product"><a class="card-image event-url"></a></div>',
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://biletino.example/search?location=İstanbul&ajax=true",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert "event-url" in result.solution.response
    assert page.goto_calls == []


@pytest.mark.anyio
async def test_ajax_get_direct_challenge_falls_back_to_browser_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct_calls = 0

    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        nonlocal direct_calls
        direct_calls += 1
        response = FakeResponse(
            status=403,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value="<html><title>Just a moment...</title></html>",
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    page.content_value = "<html><title>Example</title><body>browser ok</body></html>"

    result = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com/search?ajax=true",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert direct_calls == 1
    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == page.content_value
    assert page.goto_calls[-1]["url"] == "https://example.com/search?ajax=true"


def test_direct_http_get_percent_encodes_non_ascii_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_urls: list[str] = []

    class Headers:
        def get_content_charset(self) -> str:
            return "utf-8"

        def items(self) -> list[tuple[str, str]]:
            return [("content-type", "text/html; charset=utf-8")]

    class UrlopenResponse:
        status = 200
        headers = Headers()

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

        def geturl(self) -> str:
            return opened_urls[-1]

    def fake_open(http_request: Any, timeout: float) -> UrlopenResponse:
        opened_urls.append(http_request.full_url)
        assert timeout == 5
        return UrlopenResponse()

    monkeypatch.setattr(solver_module._HTTP_OPENER, "open", fake_open)

    response = solver_module._direct_http_get_sync(
        "https://biletino.example/search?location=İstanbul&ajax=true",
        V1Request(cmd="request.get", url="https://biletino.example"),
        5,
    )

    assert opened_urls == ["https://biletino.example/search?location=%C4%B0stanbul&ajax=true"]
    assert response.status == 200
    assert response.url == opened_urls[-1]


@pytest.mark.anyio
async def test_transient_content_collection_race_does_not_fail_request() -> None:
    class RacingPage(FakePage):
        def __init__(self, context: FakeContext) -> None:
            super().__init__(context)
            self.content_calls = 0

        async def content(self) -> str:
            self.content_calls += 1
            if self.content_calls == 1:
                raise RuntimeError(
                    "Page.content: Unable to retrieve content because the page is "
                    "navigating and changing the content."
                )
            return await super().content()

    context = FakeContext()
    page = RacingPage(context)
    context.pages.append(page)

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.response == page.content_value
    assert page.content_calls >= 2


@pytest.mark.anyio
async def test_closed_transport_solution_collection_does_not_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    closed_transport_message = (
        "unable to perform operation on <WriteUnixTransport closed=True "
        "reading=False>; the handler is closed"
    )

    class ClosedTransportContext(FakeContext):
        async def cookies(self):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"BrowserContext.cookies: {closed_transport_message}")

    class ClosedTransportPage:
        url = "https://example.com/challenge"

        async def set_extra_http_headers(self, _: dict[str, str]) -> None:
            return None

        async def goto(self, *_: object, **kwargs: object) -> FakeResponse:
            wait_until = kwargs.get("wait_until")
            raise TimeoutError(f"{wait_until} timed out")

        async def wait_for_load_state(
            self,
            state: str = "load",
            *,
            timeout: float | None = None,
        ) -> None:
            raise TimeoutError(f"{state} timed out")

        async def content(self) -> str:
            raise RuntimeError(f"Page.content: {closed_transport_message}")

        async def evaluate(self, _: str) -> str:
            raise RuntimeError(f"Page.evaluate: {closed_transport_message}")

    context = ClosedTransportContext()
    page = ClosedTransportPage()

    with caplog.at_level(logging.ERROR, logger="camouflare.solver"):
        result = await solve_request(
            V1Request(
                cmd="request.get",
                url="https://example.com/challenge",
                maxTimeout=60000,
            ),
            context=context,
            page=page,
        )

    assert result.status == "error"
    assert result.solution is not None
    assert result.solution.response == ""
    assert result.solution.cookies == []
    assert result.solution.user_agent == ""
    assert caplog.records == []


@pytest.mark.anyio
async def test_captcha_error_uses_safe_content_collection() -> None:
    class FailingProvider:
        async def solve(self, **_: object) -> str | None:
            raise RuntimeError("provider failed")

    class FlakyChallengePage(FakePage):
        def __init__(self, context: FakeContext) -> None:
            super().__init__(context)
            self.content_calls = 0

        async def content(self) -> str:
            self.content_calls += 1
            if self.content_calls == 1:
                return '<html><script src="/cdn-cgi/challenge-platform/test"></script></html>'
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is "
                "navigating and changing the content."
            )

    context = FakeContext()
    page = FlakyChallengePage(context)
    context.pages.append(page)

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        captcha_provider=FailingProvider(),
    )

    assert result.status == "error"
    assert "provider failed" in result.message
    assert result.solution is not None
    assert result.solution.response == ""
    assert page.content_calls == 2


@pytest.mark.anyio
async def test_challenge_detection_waits_for_page_to_stabilize() -> None:
    class StabilizingChallengePage(FakePage):
        def __init__(self, context: FakeContext) -> None:
            super().__init__(context)
            self.networkidle_seen = False
            self.solved = False

        async def wait_for_load_state(
            self,
            state: str = "load",
            *,
            timeout: float | None = None,
        ) -> None:
            await super().wait_for_load_state(state, timeout=timeout)
            if state == "networkidle":
                self.networkidle_seen = True

        async def content(self) -> str:
            if not self.networkidle_seen:
                raise RuntimeError(
                    "Page.content: Unable to retrieve content because the page is "
                    "navigating and changing the content."
                )
            if self.solved:
                return "<html><title>Example</title><body>ok</body></html>"
            return '<html><script src="/cdn-cgi/challenge-platform/test"></script></html>'

    class SolvingProvider:
        async def solve(self, **kwargs: object) -> str | None:
            page = kwargs["page"]
            page.solved = True  # type: ignore[attr-defined]
            return "turnstile-token"

    context = FakeContext()
    page = StabilizingChallengePage(context)
    context.pages.append(page)

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        captcha_provider=SolvingProvider(),
    )

    assert result.status == "ok"
    assert result.message == "Challenge solved!"
    assert result.solution is not None
    assert result.solution.turnstile_token == "turnstile-token"


@pytest.mark.anyio
async def test_turnstile_script_on_normal_page_is_not_treated_as_challenge() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Etkinlik Kesfet"
    page.content_value = (
        "<!DOCTYPE html><html><head>"
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" '
        "async defer></script>"
        "</head><body>Biletino content</body></html>"
    )

    result = await solve_request(
        V1Request(cmd="request.get", url="https://www.biletino.com", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.message == "Challenge not detected!"
    assert result.solution is not None
    assert "Biletino content" in result.solution.response


@pytest.mark.anyio
async def test_remaining_challenge_html_returns_error() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Just a moment..."
    page.content_value = '<html><script src="/cdn-cgi/challenge-platform/test"></script></html>'

    async def _instant_sleep(_seconds: float) -> None:
        return None

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        # The challenge never clears; the auto-clear poll would otherwise sleep the
        # whole budget, so account time instantly here.
        sleep=_instant_sleep,
    )

    assert result.status == "error"
    assert "Challenge remained" in result.message
    assert result.error_code is V1ErrorCode.CHALLENGE_FAILED
    assert result.retryable is False


@pytest.mark.anyio
async def test_captcha_provider_timeout_returns_error_envelope() -> None:
    class HangingProvider:
        async def solve(self, **_: object) -> str | None:
            await asyncio.sleep(1)
            return None

    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Just a moment..."
    page.content_value = '<html><script src="/cdn-cgi/challenge-platform/test"></script></html>'

    result = await asyncio.wait_for(
        solve_request(
            V1Request(cmd="request.get", url="https://example.com", maxTimeout=20),
            context=context,
            page=page,
            captcha_provider=HangingProvider(),
        ),
        timeout=5.0,
    )

    assert result.status == "error"
    assert "timed out" in result.message.lower()
    assert result.solution is not None


@pytest.mark.anyio
async def test_captcha_provider_exception_returns_error_envelope() -> None:
    class FailingProvider:
        async def solve(self, **_: object) -> str | None:
            raise RuntimeError("provider failed")

    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Just a moment..."
    page.content_value = '<html><script src="/cdn-cgi/challenge-platform/test"></script></html>'

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        captcha_provider=FailingProvider(),
    )

    assert result.status == "error"
    assert "provider failed" in result.message
    assert result.solution is not None
    assert result.error_code is V1ErrorCode.CHALLENGE_FAILED
    assert result.retryable is False


@pytest.mark.anyio
@pytest.mark.parametrize("scheme_url", ["file:///etc/passwd", "ftp://host/f", "data:text/html,x"])
async def test_non_http_url_scheme_is_rejected(scheme_url: str) -> None:
    context = FakeContext()
    page = await context.new_page()

    result = await solve_request(
        V1Request(cmd="request.get", url=scheme_url, maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "error"
    assert "http or https" in result.message
    assert result.error_code is V1ErrorCode.INVALID_REQUEST
    assert result.retryable is False
    assert page.goto_calls == []


@pytest.mark.anyio
async def test_final_navigation_response_status_overrides_stale_initial_response() -> None:
    # Initial goto lands on a Cloudflare interstitial (403); the challenge clears via
    # a client-side navigation that surfaces a fresh 200 main-frame response. The
    # reported status must come from the final response, not the stale interstitial.
    context = FakeContext()
    page = await context.new_page()
    page.goto_response = FakeResponse(
        status=403,
        headers={"content-type": "text/html", "cf-mitigated": "challenge"},
    )
    page.content_value = "<html><title>Example</title><body>real content</body></html>"

    original_wait = page.wait_for_load_state

    async def wait_and_navigate(state: str = "load", *, timeout: float | None = None) -> None:
        # Simulate the challenge clearing during the networkidle wait.
        page.emit_navigation_response(status=200, url="https://example.com/final")
        await original_wait(state, timeout=timeout)

    page.wait_for_load_state = wait_and_navigate  # type: ignore[assignment]

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.status == 200
    assert result.fallback_used is None


@pytest.mark.anyio
async def test_turnstile_widget_input_on_normal_page_is_not_treated_as_challenge() -> None:
    # A legitimate page that embeds a Turnstile widget injects a hidden
    # cf-turnstile-response input; it must not be misread as an unsolved challenge.
    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Contact Us"
    page.content_value = (
        "<html><head></head><body><form>"
        '<div class="cf-turnstile"></div>'
        '<input type="hidden" name="cf-turnstile-response" value="">'
        "</form>Real page body</body></html>"
    )

    result = await solve_request(
        V1Request(cmd="request.get", url="https://shop.example/contact", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.message == "Challenge not detected!"
    assert result.solution is not None
    assert "Real page body" in result.solution.response


def test_direct_http_get_falls_back_to_utf8_for_unknown_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Headers:
        def get_content_charset(self) -> str:
            return "totally-bogus-charset"

        def items(self) -> list[tuple[str, str]]:
            return [("content-type", "text/html; charset=totally-bogus-charset")]

    class OpenerResponse:
        status = 200
        headers = Headers()

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return "café".encode()

        def geturl(self) -> str:
            return "https://example.com/x?ajax=true"

    monkeypatch.setattr(
        solver_module._HTTP_OPENER, "open", lambda request, timeout: OpenerResponse()
    )

    response = solver_module._direct_http_get_sync(
        "https://example.com/x?ajax=true",
        V1Request(cmd="request.get", url="https://example.com/x?ajax=true"),
        5,
    )

    assert response.status == 200
    assert "café" in response._body


@pytest.mark.anyio
async def test_disable_media_routes_removed_when_next_request_omits_flag() -> None:
    context = FakeContext()

    first_page = await context.new_page()
    await solve_request(
        V1Request(
            cmd="request.get", url="https://example.com", maxTimeout=60000, disableMedia=True
        ),
        context=context,
        page=first_page,
    )
    assert len(context.routes) == len(MEDIA_PATTERNS)

    second_page = await context.new_page()
    await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=second_page,
    )
    assert context.routes == []


@pytest.mark.anyio
async def test_screenshot_limit_error_is_not_suppressed() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.screenshot_value = b"oversized-png"

    accepted = await solve_request(
        V1Request(
            cmd="request.get",
            url="https://example.com",
            returnScreenshot=True,
        ),
        context=context,
        page=page,
        limits=ResourceLimits(screenshot_bytes=len(page.screenshot_value)),
    )
    assert accepted.solution is not None
    assert accepted.solution.screenshot is not None

    with pytest.raises(ResourceLimitError, match="Screenshot"):
        await solve_request(
            V1Request(
                cmd="request.get",
                url="https://example.com",
                returnScreenshot=True,
            ),
            context=context,
            page=page,
            limits=ResourceLimits(screenshot_bytes=len(page.screenshot_value) - 1),
        )


@pytest.mark.anyio
async def test_response_cookie_count_limit_is_enforced() -> None:
    context = FakeContext()
    context.cookies_result = [
        {"name": f"cookie-{index}", "value": "v", "domain": "example.com"} for index in range(301)
    ]
    page = await context.new_page()

    with pytest.raises(ResourceLimitError, match="cookies"):
        await solve_request(
            V1Request(cmd="request.get", url="https://example.com"),
            context=context,
            page=page,
        )


@pytest.mark.anyio
async def test_response_cookie_byte_limit_accepts_boundary_and_rejects_plus_one() -> None:
    context = FakeContext()
    cookie_template = [{"name": "x", "value": "", "domain": "example.com"}]
    overhead = json_size(cookie_template)
    context.cookies_result = [
        {
            "name": "x",
            "value": "v" * (MAX_COOKIE_BYTES - overhead),
            "domain": "example.com",
        }
    ]
    page = await context.new_page()

    accepted = await solve_request(
        V1Request(cmd="request.get", url="https://example.com"),
        context=context,
        page=page,
        allow_direct_http_first=False,
    )
    assert accepted.status == "ok"

    context.cookies_result[0]["value"] += "v"
    with pytest.raises(ResourceLimitError, match="cookies"):
        await solve_request(
            V1Request(cmd="request.get", url="https://example.com"),
            context=context,
            page=page,
            allow_direct_http_first=False,
        )


@pytest.mark.anyio
async def test_dom_response_limit_accepts_boundary_and_rejects_plus_one() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.content_value = "x" * 8

    accepted = await solve_request(
        V1Request(cmd="request.get", url="https://example.com"),
        context=context,
        page=page,
        limits=ResourceLimits(response_body_bytes=8),
        allow_direct_http_first=False,
    )
    assert accepted.solution is not None
    assert accepted.solution.response == "x" * 8

    page.content_value += "x"
    with pytest.raises(ResourceLimitError, match="Response body"):
        await solve_request(
            V1Request(cmd="request.get", url="https://example.com"),
            context=context,
            page=page,
            limits=ResourceLimits(response_body_bytes=8),
            allow_direct_http_first=False,
        )


@pytest.mark.anyio
async def test_oversized_api_response_is_disposed_before_error() -> None:
    class ApiResponse:
        status = 200
        url = "https://example.com/data"

        def __init__(self) -> None:
            self.headers = {"content-type": "text/plain; charset=utf-8"}
            self.disposed = False

        async def body(self) -> bytes:
            return b"12345"

        async def dispose(self) -> None:
            self.disposed = True

    response = ApiResponse()

    accepted = await solver_module._raw_response_from_api_response(
        response,
        fallback_url=response.url,
        limits=ResourceLimits(response_body_bytes=5),
    )
    assert await accepted.text() == "12345"
    assert response.disposed is True

    response = ApiResponse()

    with pytest.raises(ResourceLimitError, match="Response body"):
        await solver_module._raw_response_from_api_response(
            response,
            fallback_url=response.url,
            limits=ResourceLimits(response_body_bytes=4),
        )

    assert response.disposed is True


def test_direct_http_reads_only_limit_plus_one(monkeypatch: pytest.MonkeyPatch) -> None:
    read_sizes: list[int] = []

    class Headers:
        def get_content_charset(self) -> str:
            return "utf-8"

        def items(self) -> list[tuple[str, str]]:
            return []

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            read_sizes.append(size)
            return b"x" * size

        def geturl(self) -> str:
            return "https://example.com"

    monkeypatch.setattr(
        solver_module._HTTP_OPENER,
        "open",
        lambda request, timeout: Response(),
    )

    with pytest.raises(ResourceLimitError, match="Response body"):
        solver_module._direct_http_get_sync(
            "https://example.com",
            V1Request(cmd="request.get", url="https://example.com"),
            1,
            maximum_body_bytes=4,
        )

    assert read_sizes == [5]


@pytest.mark.anyio
async def test_setup_error_returns_envelope_and_logs_internal_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class BadCookieContext(FakeContext):
        async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
            raise RuntimeError("cookies[0] must have url or domain")

    context = BadCookieContext()
    page = await context.new_page()

    with caplog.at_level(logging.ERROR, logger="camouflare.solver"):
        result = await solve_request(
            V1Request(
                cmd="request.get",
                url="https://example.com",
                maxTimeout=60000,
                cookies=[{"name": "x", "value": "y"}],
            ),
            context=context,
            page=page,
        )

    assert result.status == "error"
    assert "setup failed" in result.message.lower()
    assert result.solution is not None
    assert page.goto_calls == []
    internal = next(
        record for record in caplog.records if record.message == "Unexpected request setup error."
    )
    assert internal.exc_info is not None


@pytest.mark.anyio
async def test_nested_get_fallback_commit_crash_uses_direct_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        response = FakeResponse(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value="<html><title>Example</title><body>direct body</body></html>",
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()
    # domcontentloaded times out -> waiting on the existing navigation's commit
    # observes a transport crash -> direct HTTP fallback without replaying GET.
    page.goto_failures["domcontentloaded"] = TimeoutError("domcontentloaded timed out")
    original_wait_for_load_state = page.wait_for_load_state

    async def fail_commit_wait(
        state: str = "load",
        *,
        timeout: float | None = None,
    ) -> None:
        if state == "commit":
            page.load_states.append(state)
            raise RuntimeError("Page.wait_for_load_state: Connection closed")
        await original_wait_for_load_state(state, timeout=timeout)

    page.wait_for_load_state = fail_commit_wait  # type: ignore[method-assign]

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com/x", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert "direct body" in result.solution.response
    assert [call["wait_until"] for call in page.goto_calls] == ["domcontentloaded"]
    assert result.fallback_used is True


@pytest.mark.anyio
async def test_post_context_request_challenge_does_not_retry_with_browser_form() -> None:
    class ChallengeApiRequest:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url: str, **kwargs: Any) -> FakeResponse:
            self.calls += 1
            return FakeResponse(
                status=403,
                headers={"content-type": "text/html"},
                text_value="<html><title>Just a moment...</title></html>",
            )

    context = FakeContext()
    context.request = ChallengeApiRequest()  # type: ignore[attr-defined]
    page = await context.new_page()
    page.content_value = "<html><title>Example</title><body>form result</body></html>"

    result = await solve_request(
        V1Request(
            cmd="request.post",
            url="https://example.com/login",
            postData="user=a&pass=b",
            maxTimeout=60000,
        ),
        context=context,
        page=page,
    )

    assert context.request.calls == 1  # type: ignore[attr-defined]
    assert result.status == "error"
    assert result.error_code is V1ErrorCode.CHALLENGE_FAILED
    assert page.posted_form is None
    assert result.solution is not None
    assert "Just a moment" in result.solution.response


@pytest.mark.anyio
async def test_submit_post_form_preserves_duplicate_field_keys() -> None:
    class CapturePage:
        def __init__(self) -> None:
            self.html = ""
            self.evaluated: list[str] = []

        async def set_content(self, html: str) -> None:
            self.html = html

        async def evaluate(self, script: str) -> str:
            self.evaluated.append(script)
            return ""

        async def wait_for_load_state(self, state: str = "load", *, timeout: float | None = None):
            return None

    page = CapturePage()  # no posted_form attr -> builds the real hidden HTML form

    await _submit_post_form(
        page,  # type: ignore[arg-type]
        V1Request(
            cmd="request.post",
            url="https://example.com/f",
            postData="cat=books&cat=toys&page=1",
            maxTimeout=60000,
        ),
        TimeoutTimer(60000),
    )

    # Both repeated cat fields must survive; a dict would have dropped 'books'.
    assert page.html.count('name="cat"') == 2
    assert 'value="books"' in page.html
    assert 'value="toys"' in page.html


def test_direct_http_get_sends_matching_cookies_as_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Headers:
        def get_content_charset(self) -> str:
            return "utf-8"

        def items(self) -> list[tuple[str, str]]:
            return [("content-type", "text/html; charset=utf-8")]

    class OpenerResponse:
        status = 200
        headers = Headers()

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

        def geturl(self) -> str:
            return "https://example.com/x?ajax=true"

    def fake_open(http_request: Any, timeout: float) -> OpenerResponse:
        captured["headers"] = dict(http_request.headers)
        return OpenerResponse()

    monkeypatch.setattr(solver_module._HTTP_OPENER, "open", fake_open)

    solver_module._direct_http_get_sync(
        "https://example.com/x?ajax=true",
        V1Request(
            cmd="request.get",
            url="https://example.com/x?ajax=true",
            cookies=[
                {"name": "sid", "value": "abc", "domain": "example.com"},
                {"name": "other", "value": "z", "domain": "other.test"},
            ],
        ),
        5,
    )

    # urllib title-cases header names; only the example.com cookie should be sent.
    assert captured["headers"].get("Cookie") == "sid=abc"


@pytest.mark.anyio
async def test_direct_http_fallback_not_shadowed_by_stale_browser_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # goto surfaces a 403 interstitial response (recorded by the response listener)
    # and then the transport dies; the direct-HTTP fallback returns the real 200
    # body. The stale browser response must not shadow that RawResponse.
    async def direct_get(url: str, request: V1Request, timer: TimeoutTimer) -> FakeResponse:
        response = FakeResponse(
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text_value="<html><title>Example</title><body>REAL DIRECT BODY</body></html>",
        )
        response.raw_body = True  # type: ignore[attr-defined]
        response.url = url  # type: ignore[attr-defined]
        return response

    monkeypatch.setattr(solver_module, "_direct_http_get", direct_get)

    context = FakeContext()
    page = await context.new_page()

    async def goto_emit_then_crash(url: str, **kwargs: Any) -> FakeResponse:
        page.emit_navigation_response(status=403, url=url)
        raise RuntimeError("Page.goto: Connection closed while reading from the driver")

    page.goto = goto_emit_then_crash  # type: ignore[assignment]

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com/x", maxTimeout=60000),
        context=context,
        page=page,
    )

    assert result.status == "ok"
    assert result.solution is not None
    assert result.solution.status == 200
    assert "REAL DIRECT BODY" in result.solution.response


@pytest.mark.anyio
async def test_provider_prepare_runs_before_navigation_and_clears_challenge() -> None:
    events: list[str] = []

    class OrderedChallengePage(FakePage):
        def __init__(self, context: FakeContext) -> None:
            super().__init__(context)
            self.solved = False

        async def goto(self, url: str, **kwargs: Any) -> FakeResponse:
            events.append("goto")
            return await super().goto(url, **kwargs)

        async def title(self) -> str:
            return "Example" if self.solved else "Just a moment..."

        async def content(self) -> str:
            if self.solved:
                return "<html><title>Example</title><body>ok</body></html>"
            return (
                "<html><title>Just a moment...</title>"
                '<script src="/cdn-cgi/challenge-platform/x"></script></html>'
            )

    class OrderedProvider:
        @asynccontextmanager
        async def prepare(self, *, page: Any):  # type: ignore[no-untyped-def]
            events.append("prepare_enter")
            try:
                yield
            finally:
                events.append("prepare_exit")

        async def solve(self, *, page: Any, request: Any, timer: Any) -> str | None:
            events.append("solve")
            page.solved = True
            return None

    context = FakeContext()
    page = OrderedChallengePage(context)
    context.pages.append(page)

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        captcha_provider=OrderedProvider(),
    )

    assert result.status == "ok"
    assert result.message == "Challenge solved!"
    # prepare() must be entered before the page is navigated, then solve runs, then
    # the prepare context unwinds last.
    assert events[0] == "prepare_enter"
    assert events.index("prepare_enter") < events.index("goto") < events.index("solve")
    assert events[-1] == "prepare_exit"


@pytest.mark.anyio
async def test_provider_cleanup_timeout_does_not_block_completed_solve() -> None:
    cleanup_started = asyncio.Event()
    never_finishes = asyncio.Event()

    class HangingCleanupProvider:
        @asynccontextmanager
        async def prepare(self, *, page: Any):  # type: ignore[no-untyped-def]
            try:
                yield
            finally:
                cleanup_started.set()
                await never_finishes.wait()

        async def solve(self, **_: object) -> str | None:
            return None

    context = FakeContext()
    page = await context.new_page()
    result = await asyncio.wait_for(
        solve_request(
            V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
            context=context,
            page=page,
            captcha_provider=HangingCleanupProvider(),
            cleanup_timeout_seconds=0.01,
        ),
        timeout=0.5,
    )

    assert cleanup_started.is_set()
    assert result.status == "ok"


class _NoneProvider:
    async def solve(self, **_: object) -> str | None:
        return None


@pytest.mark.anyio
async def test_challenge_auto_clears_while_waiting() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Just a moment..."
    page.content_value = (
        "<html><title>Just a moment...</title>"
        '<script src="/cdn-cgi/challenge-platform/x"></script></html>'
    )

    async def clearing_sleep(_seconds: float) -> None:
        # Simulate the interstitial clearing on its own after one poll interval.
        page.title_value = "Example"
        page.content_value = "<html><title>Example</title><body>ok</body></html>"

    result = await solve_request(
        V1Request(cmd="request.get", url="https://example.com", maxTimeout=60000),
        context=context,
        page=page,
        captcha_provider=_NoneProvider(),
        sleep=clearing_sleep,
    )

    # A no-op solver that leaves the page challenged must NOT fail immediately; the
    # auto-clear wait lets a passive interstitial resolve into a success.
    assert result.status == "ok"
    assert result.message == "Challenge solved!"


@pytest.mark.anyio
async def test_media_blocking_removed_when_challenge_detected() -> None:
    context = FakeContext()
    page = await context.new_page()
    page.title_value = "Just a moment..."
    page.content_value = (
        "<html><title>Just a moment...</title>"
        '<script src="/cdn-cgi/challenge-platform/x"></script></html>'
    )

    async def clearing_sleep(_seconds: float) -> None:
        page.title_value = "Example"
        page.content_value = "<html><title>Example</title><body>ok</body></html>"

    result = await solve_request(
        V1Request(
            cmd="request.get", url="https://example.com", maxTimeout=60000, disableMedia=True
        ),
        context=context,
        page=page,
        captcha_provider=_NoneProvider(),
        sleep=clearing_sleep,
    )

    assert result.status == "ok"
    # Media routes are installed at setup (disableMedia=True) then torn down once a
    # challenge is detected, because block_images breaks Cloudflare challenge solving.
    assert context.routes == []
