from __future__ import annotations

import base64
import codecs
import logging
import re
from collections.abc import Awaitable, Callable
from html import unescape
from http import HTTPStatus
from typing import Any, cast

from camouflare.limits import (
    MAX_COOKIE_BYTES,
    MAX_COOKIES,
    ResourceLimitError,
    ResourceLimits,
    ensure_bytes_size,
    ensure_json_size,
    ensure_text_size,
    utf8_size,
)
from camouflare.metrics import observe_payload_size
from camouflare.models import Solution, V1Request
from camouflare.protocols import BrowserContextLike, PageLike, ResponseLike

BEST_EFFORT_BROWSER_ERROR_MARKERS = (
    "page is navigating and changing the content",
    "execution context was destroyed",
    "writeunixtransport closed",
    "transport closed",
    "connection closed",
    "the handler is closed",
    "browser has been closed",
    "browser closed",
    "target closed",
    "target page, context or browser has been closed",
)

logger = logging.getLogger(__name__)


async def collect_solution(
    request: V1Request,
    *,
    context: BrowserContextLike,
    page: PageLike,
    page_response: ResponseLike | None,
    content: str,
    turnstile_token: str | None,
    limits: ResourceLimits,
) -> Solution:
    ensure_text_size(content, limits.response_body_bytes, label="Response body")
    observe_payload_size("response", utf8_size(content))
    cookies = await safe_context_cookies(context)
    if len(cookies) > MAX_COOKIES:
        raise ResourceLimitError(
            f"Response cookies exceed the configured {MAX_COOKIES}-item limit."
        )
    ensure_json_size(cookies, MAX_COOKIE_BYTES, label="Response cookies")
    status = getattr(page_response, "status", 0 if page_response is None else HTTPStatus.OK)
    user_agent = await safe_user_agent(page, request)
    current_page_url = page_url(page)
    url = (
        current_page_url
        if current_page_url and current_page_url != "about:blank"
        else response_url(page_response)
    )

    if request.return_only_cookies:
        return Solution(
            url=url,
            status=int(status),
            cookies=cookies,
            user_agent=user_agent,
            turnstile_token=turnstile_token,
        )

    screenshot = None
    if request.return_screenshot:
        try:
            screenshot_bytes = await page.screenshot(type="png")
            ensure_bytes_size(
                screenshot_bytes,
                limits.screenshot_bytes,
                label="Screenshot",
            )
            observe_payload_size("screenshot", len(screenshot_bytes))
            screenshot = base64.b64encode(screenshot_bytes).decode("ascii")
        except ResourceLimitError:
            raise
        except Exception:
            logger.warning("Failed to capture screenshot.", exc_info=True)

    return Solution(
        url=url,
        status=int(status),
        headers=dict(getattr(page_response, "headers", {}) or {}),
        response=content,
        cookies=cookies,
        user_agent=user_agent,
        screenshot=screenshot,
        turnstile_token=turnstile_token,
    )


async def safe_context_cookies(context: BrowserContextLike) -> list[dict[str, Any]]:
    try:
        return await context.cookies()
    except Exception as exc:
        log_best_effort_collection_failure("Failed to collect browser cookies.", exc)
        return []


async def safe_page_title(page: PageLike) -> str:
    try:
        return await page.title()
    except Exception as exc:
        log_best_effort_collection_failure("Failed to collect page title.", exc)
        return ""


async def safe_page_content(page: PageLike, limits: ResourceLimits) -> str:
    try:
        content = await page.content()
        ensure_text_size(content, limits.response_body_bytes, label="Response body")
        return content
    except ResourceLimitError:
        raise
    except Exception as exc:
        log_best_effort_collection_failure("Failed to collect partial page content.", exc)
        return ""


async def response_text_or_page_content(
    page: PageLike,
    page_response: ResponseLike | None,
    limits: ResourceLimits,
) -> str:
    if should_return_raw_response_body(page_response):
        body_reader = getattr(page_response, "body", None)
        text_reader = getattr(page_response, "text", None)
        if callable(body_reader):
            try:
                raw_body = await cast(Callable[[], Awaitable[bytes]], body_reader)()
                ensure_bytes_size(
                    raw_body,
                    limits.response_body_bytes,
                    label="Response body",
                )
                headers = getattr(page_response, "headers", {}) or {}
                return raw_body.decode(response_charset(headers), errors="replace")
            except ResourceLimitError:
                raise
            except Exception:
                logger.exception("Failed to collect raw response body.")
        elif callable(text_reader):
            try:
                content = await cast(Callable[[], Awaitable[str]], text_reader)()
                ensure_text_size(
                    content,
                    limits.response_body_bytes,
                    label="Response body",
                )
                return content
            except ResourceLimitError:
                raise
            except Exception:
                logger.exception("Failed to collect raw response body.")
    return await safe_page_content(page, limits)


def should_return_raw_response_body(page_response: ResponseLike | None) -> bool:
    if page_response is None:
        return False
    if getattr(page_response, "raw_body", False):
        return True
    headers = getattr(page_response, "headers", {}) or {}
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = str(value)
            break
    mime_type = content_type.split(";", 1)[0].strip().lower()
    if not mime_type:
        return False
    return (
        mime_type in {"application/json", "application/xml", "text/plain", "text/xml"}
        or mime_type.endswith("+json")
        or mime_type.endswith("+xml")
    )


async def safe_user_agent(page: PageLike, request: V1Request) -> str:
    try:
        return str(await page.evaluate("navigator.userAgent"))
    except Exception as exc:
        log_best_effort_collection_failure("Failed to evaluate navigator.userAgent.", exc)
        return request.target_user_agent() or ""


def page_url(page: PageLike) -> str:
    try:
        return str(getattr(page, "url", ""))
    except Exception:
        return ""


def response_url(page_response: ResponseLike | None) -> str:
    try:
        return str(getattr(page_response, "url", ""))
    except Exception:
        return ""


def html_title(content: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return ""
    return unescape(re.sub(r"\s+", " ", match.group(1))).strip()


def log_best_effort_collection_failure(message: str, exc: Exception) -> None:
    if is_best_effort_browser_error(exc):
        logger.debug("%s %s", message, exc)
        return
    logger.exception(message)


def is_best_effort_browser_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in BEST_EFFORT_BROWSER_ERROR_MARKERS)


def navigation_error_message(
    exc: Exception,
    *,
    page: PageLike,
    page_response: ResponseLike | None,
) -> str:
    details = [f"Navigation failed: {exc}"]
    current_url = page_url(page)
    if current_url:
        details.append(f"current_url={current_url}")
    status = getattr(page_response, "status", None)
    if status is not None:
        details.append(f"response_status={status}")
    return "; ".join(details)


def response_charset(headers: Any) -> str:
    candidate = ""
    get_content_charset = getattr(headers, "get_content_charset", None)
    if get_content_charset is not None:
        charset = get_content_charset()
        if charset:
            candidate = str(charset)
    if not candidate:
        content_type = ""
        get = getattr(headers, "get", None)
        if get is not None:
            content_type = str(get("content-type", "") or get("Content-Type", ""))
        match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1)
    if not candidate:
        return "utf-8"
    try:
        codecs.lookup(candidate)
    except LookupError:
        return "utf-8"
    return candidate
