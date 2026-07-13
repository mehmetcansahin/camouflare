from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from camouflare.captcha import CaptchaProvider
from camouflare.limits import ResourceLimitError, ResourceLimits, ensure_text_size
from camouflare.models import V1Request
from camouflare.protocols import PageLike
from camouflare.solution import html_title, safe_page_content, safe_page_title
from camouflare.timer import TimeoutTimer

CHALLENGE_TITLES = {"just a moment...", "ddos-guard"}
CHALLENGE_MARKERS = (
    "/cdn-cgi/challenge-platform/",
    "cf-challenge",
)
CHALLENGE_CLEAR_POLL_MS = 1000

Sleep = Callable[[float], Awaitable[None]]


class ChallengeSolveError(RuntimeError):
    pass


class RequestTimeoutError(RuntimeError):
    pass


async def solve_challenge(
    provider: CaptchaProvider,
    *,
    page: PageLike,
    request: V1Request,
    timer: TimeoutTimer,
) -> str | None:
    try:
        return await asyncio.wait_for(
            provider.solve(page=page, request=request, timer=timer),
            timeout=timer.remaining_seconds,
        )
    except TimeoutError as exc:
        raise ChallengeSolveError("Challenge solve timed out.") from exc
    except Exception as exc:
        raise ChallengeSolveError(f"Challenge solve failed: {exc}") from exc


async def wait_requested(seconds: int, *, sleep: Sleep, timer: TimeoutTimer) -> None:
    try:
        await asyncio.wait_for(sleep(seconds), timeout=timer.remaining_seconds)
    except TimeoutError as exc:
        raise RequestTimeoutError("Request timed out during waitInSeconds.") from exc


def title_is_challenge(title: str) -> bool:
    return title.strip().lower() in CHALLENGE_TITLES


def content_has_challenge_markers(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS) or title_is_challenge(
        html_title(content)
    )


async def challenge_detected(page: PageLike, limits: ResourceLimits) -> bool:
    if title_is_challenge(await safe_page_title(page)):
        return True
    return content_has_challenge_markers(await safe_page_content(page, limits))


async def challenge_state(page: PageLike, limits: ResourceLimits) -> str:
    """Return ``present``, ``cleared``, or ``unknown`` for the current page."""
    if title_is_challenge(await safe_page_title(page)):
        return "present"
    try:
        content = await page.content()
        ensure_text_size(
            content,
            limits.response_body_bytes,
            label="Response body",
        )
    except ResourceLimitError:
        raise
    except Exception:
        return "unknown"
    return "present" if content_has_challenge_markers(content) else "cleared"


async def wait_for_challenge_cleared(
    page: PageLike,
    timer: TimeoutTimer,
    *,
    limits: ResourceLimits,
    sleep: Sleep,
) -> bool:
    """Poll until the challenge positively clears or the timeout budget is spent."""
    budget_ms = timer.remaining_ms
    waited_ms = 0.0
    while True:
        if await challenge_state(page, limits) == "cleared":
            return True
        if waited_ms >= budget_ms or timer.remaining_ms <= 0:
            return False
        step_ms = min(CHALLENGE_CLEAR_POLL_MS, budget_ms - waited_ms)
        await sleep(step_ms / 1000)
        waited_ms += step_ms


def challenge_markers_remain(title: str, content: str) -> bool:
    return title_is_challenge(title) or content_has_challenge_markers(content)
