from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, TypedDict


class BrowserProxy(TypedDict, total=False):
    server: str
    username: str
    password: str


class ContextOptions(TypedDict, total=False):
    no_viewport: bool
    proxy: BrowserProxy
    user_agent: str


class MainFrameResponseHolder(TypedDict):
    value: ResponseLike | None


class AsyncClose(Protocol):
    async def close(self) -> None: ...


class AsyncCleanup(Protocol):
    def __call__(self) -> Awaitable[None]: ...


class ResponseLike(Protocol):
    status: int
    headers: Mapping[str, Any]
    url: str

    async def text(self) -> str: ...


class APIResponseLike(ResponseLike, Protocol):
    async def body(self) -> bytes: ...

    def dispose(self) -> Awaitable[None] | None: ...


class RouteRequestLike(Protocol):
    headers: Mapping[str, str]

    async def all_headers(self) -> Mapping[str, str]: ...


class RouteLike(Protocol):
    request: RouteRequestLike

    async def abort(self) -> None: ...

    async def continue_(
        self,
        *,
        method: str,
        post_data: str,
        headers: Mapping[str, str],
    ) -> None: ...


class RequestContextLike(Protocol):
    async def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: int,
    ) -> APIResponseLike: ...


class PageLike(Protocol):
    url: str
    main_frame: Any

    async def goto(self, url: str, **kwargs: Any) -> ResponseLike | None: ...

    async def content(self) -> str: ...

    async def title(self) -> str: ...

    async def evaluate(self, expression: str) -> Any: ...

    async def screenshot(self, **kwargs: Any) -> bytes: ...

    async def set_content(self, html: str) -> None: ...

    async def set_extra_http_headers(self, headers: Mapping[str, str]) -> None: ...

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None: ...

    async def close(self) -> None: ...

    async def route(
        self,
        url: str,
        handler: Callable[[RouteLike], Awaitable[None]],
        **kwargs: Any,
    ) -> None: ...

    def on(self, event: str, handler: Callable[[ResponseLike], None]) -> None: ...


class BrowserContextLike(Protocol):
    request: RequestContextLike

    async def new_page(self) -> PageLike: ...

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None: ...

    async def cookies(self) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...

    async def route(
        self,
        url: str,
        handler: Callable[[RouteLike], Awaitable[None]],
    ) -> None: ...

    async def unroute(
        self,
        url: str,
        handler: Callable[[RouteLike], Awaitable[None]],
    ) -> None: ...


class BrowserLike(Protocol):
    async def new_context(self, **kwargs: Any) -> BrowserContextLike: ...

    async def close(self) -> None: ...


BrowserFactory = Callable[[], Awaitable[BrowserLike]]


class ProxyLeaseLike(Protocol):
    configured_proxy: BrowserProxy | None
    browser_proxy: BrowserProxy | None

    async def close(self) -> None: ...


class PersistentContextLeaseLike(Protocol):
    context: BrowserContextLike

    async def close(self) -> None: ...


class ContextLeaseLike(Protocol):
    context: BrowserContextLike


class ContextLeaseFactory(Protocol):
    def lease_context(self, **kwargs: Any) -> AbstractAsyncContextManager[ContextLeaseLike]: ...

    async def create_persistent_context(self, **kwargs: Any) -> PersistentContextLeaseLike: ...
