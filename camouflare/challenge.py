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
# With the default provider the clearance poll is the only thing that lets a
# browser-side challenge finish, so it may use the request's remaining budget.
# It never consumes the last reserve: the caller still needs time to classify
# the result, collect the requested solution fields, and close browser resources
# before the outer request supervisor fires.
CHALLENGE_RESULT_RESERVE_MS = 1000

_CHALLENGE_DOM_MARKERS_SCRIPT = """
() => Boolean(document.querySelector(
  '[src*="/cdn-cgi/challenge-platform/"], '
  + '[href*="/cdn-cgi/challenge-platform/"], '
  + '[id*="cf-challenge"], [class*="cf-challenge"]'
))
"""

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


async def challenge_detected(
    page: PageLike,
    limits: ResourceLimits,
    *,
    include_content: bool = True,
) -> bool:
    if title_is_challenge(await safe_page_title(page)):
        return True
    if not include_content:
        try:
            return bool(await page.evaluate(_CHALLENGE_DOM_MARKERS_SCRIPT))
        except Exception:
            return False
    return content_has_challenge_markers(await safe_page_content(page, limits))


async def challenge_state(
    page: PageLike,
    limits: ResourceLimits,
    *,
    include_content: bool = True,
) -> str:
    """Return ``present``, ``cleared``, or ``unknown`` for the current page."""
    if title_is_challenge(await safe_page_title(page)):
        return "present"
    if not include_content:
        try:
            detected = bool(await page.evaluate(_CHALLENGE_DOM_MARKERS_SCRIPT))
        except Exception:
            return "unknown"
        return "present" if detected else "cleared"
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
    include_content: bool = True,
) -> bool:
    """Poll for clearance until only the request's finalization reserve remains."""
    if await challenge_state(page, limits, include_content=include_content) == "cleared":
        return True

    # ``waited_ms`` bounds injected sleeps that do not advance the timer; the
    # timer bounds real waits whose page inspections take wall-clock time.
    budget_ms = max(0, timer.remaining_ms - CHALLENGE_RESULT_RESERVE_MS)
    waited_ms = 0
    while waited_ms < budget_ms:
        step_ms = min(
            CHALLENGE_CLEAR_POLL_MS,
            budget_ms - waited_ms,
            timer.remaining_ms - CHALLENGE_RESULT_RESERVE_MS,
        )
        if step_ms <= 0:
            return False
        await sleep(step_ms / 1000)
        waited_ms += step_ms
        if await challenge_state(page, limits, include_content=include_content) == "cleared":
            return True
    return False


def challenge_markers_remain(title: str, content: str) -> bool:
    return title_is_challenge(title) or content_has_challenge_markers(content)
