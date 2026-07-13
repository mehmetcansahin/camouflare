from __future__ import annotations

import asyncio
from typing import Any


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        text_value: str = "",
    ) -> None:
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self.text_value = text_value

    async def text(self) -> str:
        return self.text_value


class FakeRoute:
    def __init__(self) -> None:
        self.aborted = False

    async def abort(self) -> None:
        self.aborted = True


class FakeRequest:
    def __init__(self, *, navigation: bool = True) -> None:
        self._navigation = navigation

    def is_navigation_request(self) -> bool:
        return self._navigation


class FakeNavResponse:
    """A response delivered through the page ``response`` event (client-side nav)."""

    def __init__(
        self,
        *,
        frame: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "",
        navigation: bool = True,
    ) -> None:
        self.frame = frame
        self.status = status
        self.headers = headers or {"content-type": "text/html"}
        self.url = url
        self.request = FakeRequest(navigation=navigation)


class FakeContext:
    def __init__(
        self,
        browser: FakeBrowser | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.browser = browser
        self.options = options or {}
        self.pages: list[FakePage] = []
        self.cookies_added: list[dict[str, Any]] = []
        self.routes: list[tuple[str, Any]] = []
        self.closed = False
        self.cookies_result = [
            {
                "name": "cf_clearance",
                "value": "clear",
                "domain": ".example.com",
                "path": "/",
            }
        ]
        self.fail_close = False

    async def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.cookies_added.extend(cookies)

    async def cookies(self) -> list[dict[str, Any]]:
        return self.cookies_result

    async def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    async def unroute(self, pattern: str, handler: Any) -> None:
        self.routes = [
            (route_pattern, route_handler)
            for route_pattern, route_handler in self.routes
            if not (route_pattern == pattern and route_handler == handler)
        ]

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("context close failed")


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.url = "about:blank"
        self.goto_calls: list[dict[str, Any]] = []
        self.load_states: list[str] = []
        self.wait_failures: set[str] = set()
        self.title_value = "Example"
        self.content_value = "<html><title>Example</title><body>ok</body></html>"
        self.evaluated: list[str] = []
        self.events: list[str] = []
        self.extra_http_headers_calls: list[dict[str, str]] = []
        self.init_scripts: list[str] = []
        self.screenshot_value = b"png-bytes"
        self.posted_form: dict[str, str] | None = None
        self.goto_failures: dict[str, Exception] = {}
        self.goto_response: FakeResponse | None = None
        self.closed = False
        self.main_frame = object()
        self._response_handlers: list[Any] = []

    async def goto(
        self,
        url: str,
        *,
        timeout: float | None = None,
        wait_until: str | None = None,
        referer: str | None = None,
    ) -> FakeResponse:
        self.url = url
        self.events.append("goto")
        self.goto_calls.append(
            {"url": url, "timeout": timeout, "wait_until": wait_until, "referer": referer}
        )
        failure = self.goto_failures.get(wait_until or "")
        if failure is not None:
            raise failure
        return self.goto_response or FakeResponse()

    async def wait_for_load_state(
        self,
        state: str = "load",
        *,
        timeout: float | None = None,
    ) -> None:
        self.load_states.append(state)
        if state in self.wait_failures:
            raise TimeoutError(f"{state} timed out")

    async def title(self) -> str:
        return self.title_value

    async def content(self) -> str:
        return self.content_value

    async def evaluate(self, script: str) -> str:
        self.evaluated.append(script)
        if "navigator.userAgent" in script:
            return self.context.options.get("user_agent", "FakeBrowser/1.0")
        return ""

    async def screenshot(self, *, type: str = "png") -> bytes:
        return self.screenshot_value

    async def set_content(self, html: str) -> None:
        self.content_value = html

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.events.append("headers")
        self.extra_http_headers_calls.append(headers)

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._response_handlers.append(handler)

    def emit_navigation_response(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        url: str = "",
        navigation: bool = True,
    ) -> None:
        response = FakeNavResponse(
            frame=self.main_frame,
            status=status,
            headers=headers,
            url=url,
            navigation=navigation,
        )
        for handler in self._response_handlers:
            handler(response)

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts: list[FakeContext] = []
        self.context_options: list[dict[str, Any]] = []
        self.closed = False
        self.fail_new_context = False

    async def new_context(self, **options: Any) -> FakeContext:
        if self.fail_new_context:
            raise RuntimeError("new context failed")
        self.context_options.append(options)
        context = FakeContext(self, options)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True


class FakeBrowserFactory:
    def __init__(self) -> None:
        self.created: list[FakeBrowser] = []

    async def __call__(self) -> FakeBrowser:
        browser = FakeBrowser()
        self.created.append(browser)
        return browser


class DelayedFakeBrowserFactory(FakeBrowserFactory):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def __call__(self) -> FakeBrowser:
        await asyncio.sleep(self.delay)
        return await super().__call__()


class DelayedFakeSessionContext(FakeContext):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    async def new_page(self) -> FakePage:
        self.events.append("new_page_start")
        await asyncio.sleep(0.01)
        self.events.append("new_page_end")
        return await super().new_page()
