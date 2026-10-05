from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from http import HTTPStatus
from string import Template
from typing import Literal, cast

from camouflare.captcha import CaptchaProvider
from camouflare.limits import ResourceLimitError, ResourceLimits, ensure_text_size
from camouflare.models import V1Request
from camouflare.protocols import PageLike
from camouflare.solution import html_title, safe_page_title
from camouflare.timer import TimeoutTimer

ChallengeState = Literal["present", "cleared", "blocked", "unknown"]

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

# A hard block is a terminal bot/WAF decision, not a challenge a solver can pass.
# It is recognized from the block page's element structure (never script text,
# comments, escaped markup or bare wording) and corroborated by the block page
# title or an HTTP 403, so an ordinary page quoting the wording is not a block.
# Block pages are small; only a bounded prefix of a document is ever parsed.
_HARD_BLOCK_PROBE_CHARS = 65_536
_HARD_BLOCK_CONTAINER_ID = "cf-error-details"
_HARD_BLOCK_HEADLINE_KEY = "block_headline"
_HARD_BLOCK_HEADLINE = "sorry, you have been blocked"
_HARD_BLOCK_CODE_CLASS = "cf-error-code"
# Cloudflare "Access denied" pages for IP/ASN/country/browser-signature bans and
# firewall-rule blocks. Origin, DNS and rate-limit errors share the page layout
# but are not bot blocks.
_HARD_BLOCK_ERROR_CODES = frozenset(
    {"1005", "1006", "1007", "1008", "1009", "1010", "1012", "1020"}
)
_HARD_BLOCK_TITLE = "attention required! | cloudflare"
_ACCESS_DENIED_TITLE_PREFIX = "access denied | "
_ACCESS_DENIED_TITLE_SUFFIX = " used cloudflare to restrict access"
# Polling ends once no solvable challenge remains on the page.
_RESOLVED_STATES: frozenset[ChallengeState] = frozenset({"cleared", "blocked"})
_VOID_HTML_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# Cookies-only requests never serialize the page, so markers come from bounded
# DOM queries; script text is not an element and cannot match.
_PAGE_MARKERS_SCRIPT = Template(
    r"""
() => {
  const normalized = (node) => (node.textContent || '').replace(/\s+/g, ' ').trim();
  const challenge = Boolean(document.querySelector(
    '[src*="/cdn-cgi/challenge-platform/"], '
    + '[href*="/cdn-cgi/challenge-platform/"], '
    + '[id*="cf-challenge"], [class*="cf-challenge"]'
  ));
  const container = document.getElementById($container_id);
  if (container === null) {
    return {block: false, challenge};
  }
  const code = container.querySelector($code_selector);
  let block = container.querySelector($headline_selector) !== null
    || (code !== null && $error_codes.includes(normalized(code)));
  if (!block) {
    for (const heading of container.querySelectorAll('h1')) {
      if (normalized(heading).toLowerCase() === $headline) {
        block = true;
        break;
      }
    }
  }
  return {block, challenge};
}
"""
).substitute(
    container_id=json.dumps(_HARD_BLOCK_CONTAINER_ID),
    code_selector=json.dumps(f".{_HARD_BLOCK_CODE_CLASS}"),
    headline_selector=json.dumps(f'[data-translate="{_HARD_BLOCK_HEADLINE_KEY}"]'),
    headline=json.dumps(_HARD_BLOCK_HEADLINE),
    error_codes=json.dumps(sorted(_HARD_BLOCK_ERROR_CODES)),
)

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


def content_is_hard_block(content: str, *, status: int | None = None) -> bool:
    """Whether ``content`` is a terminal bot/WAF block document.

    ``status`` is the HTTP status of the response that delivered ``content``.
    """
    if content.find(_HARD_BLOCK_CONTAINER_ID, 0, _HARD_BLOCK_PROBE_CHARS) < 0:
        return False
    prefix = content[:_HARD_BLOCK_PROBE_CHARS]
    if not prefix.lstrip("\ufeff \t\r\n").startswith("<"):
        # A JSON/text response can quote HTML without being that document.
        return False
    parser = _HardBlockStructureParser()
    try:
        parser.feed(prefix)
        parser.close()
    except Exception:
        # Markup the parser cannot read is not evidence of a block page.
        return False
    return parser.blocked and _hard_block_corroborated(parser.title or "", status)


def content_state(content: str, *, status: int | None = None) -> ChallengeState:
    """Classify a response document; a hard block wins over challenge markers.

    Block pages can still load challenge-platform scripts, but no solver can
    turn them into the requested content.
    """
    if content_is_hard_block(content, status=status):
        return "blocked"
    return "present" if content_has_challenge_markers(content) else "cleared"


async def challenge_state(
    page: PageLike,
    limits: ResourceLimits,
    *,
    include_content: bool = True,
    status: int | None = None,
) -> ChallengeState:
    """Return ``blocked``, ``present``, ``cleared``, or ``unknown`` for the current page.

    ``status`` is the page's main-frame HTTP status when known. Without
    ``include_content`` the page is inspected through bounded DOM queries only.
    """
    title = await safe_page_title(page)
    if not include_content:
        try:
            markers = await page.evaluate(_PAGE_MARKERS_SCRIPT)
        except Exception:
            return "present" if title_is_challenge(title) else "unknown"
        flags = cast("dict[str, object]", markers) if isinstance(markers, dict) else {}
        if flags.get("block") and _hard_block_corroborated(title, status):
            return "blocked"
        return "present" if title_is_challenge(title) or flags.get("challenge") else "cleared"
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
        return "present" if title_is_challenge(title) else "unknown"
    state = content_state(content, status=status)
    return "present" if state == "cleared" and title_is_challenge(title) else state


async def wait_for_challenge_cleared(
    page: PageLike,
    timer: TimeoutTimer,
    *,
    limits: ResourceLimits,
    sleep: Sleep,
    include_content: bool = True,
) -> bool:
    """Poll until no solvable challenge remains or only the finalization reserve does.

    A challenge replaced by a hard block also ends the wait: polling cannot
    clear it, and the caller classifies the final document.
    """
    if await challenge_state(page, limits, include_content=include_content) in _RESOLVED_STATES:
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
        if await challenge_state(page, limits, include_content=include_content) in _RESOLVED_STATES:
            return True
    return False


def _hard_block_corroborated(title: str, status: int | None) -> bool:
    """Whether the block page title or HTTP status confirms the block structure."""
    if status == HTTPStatus.FORBIDDEN:
        return True
    normalized = " ".join(title.split()).lower()
    return normalized == _HARD_BLOCK_TITLE or (
        normalized.startswith(_ACCESS_DENIED_TITLE_PREFIX)
        and normalized.endswith(_ACCESS_DENIED_TITLE_SUFFIX)
    )


class _HardBlockStructureParser(HTMLParser):
    """Find block-page elements; script text and comments never become elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.blocked = False
        self._container_tag: str | None = None
        self._container_depth = 0
        self._title_parts: list[str] | None = None
        self._heading_parts: list[str] | None = None
        self._code_tag: str | None = None
        self._code_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag == "title" and self.title is None and self._title_parts is None:
            self._title_parts = []
            return
        if self._container_tag is not None and tag == self._container_tag:
            self._container_depth += 1
        elif (
            self._container_tag is None
            and tag not in _VOID_HTML_TAGS
            and attributes.get("id") == _HARD_BLOCK_CONTAINER_ID
        ):
            self._container_tag = tag
            self._container_depth = 1
        if self._container_tag is None:
            return
        if attributes.get("data-translate") == _HARD_BLOCK_HEADLINE_KEY:
            self.blocked = True
        if tag == "h1":
            self._heading_parts = []
        if self._code_tag is None and _HARD_BLOCK_CODE_CLASS in attributes.get("class", "").split():
            self._code_tag = tag
            self._code_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title_parts is not None:
            self.title = "".join(self._title_parts)
            self._title_parts = None
        if tag == "h1" and self._heading_parts is not None:
            heading = " ".join("".join(self._heading_parts).split()).lower()
            self.blocked = self.blocked or heading == _HARD_BLOCK_HEADLINE
            self._heading_parts = None
        if tag == self._code_tag:
            code = "".join(self._code_parts).strip()
            self.blocked = self.blocked or code in _HARD_BLOCK_ERROR_CODES
            self._code_tag = None
        if tag == self._container_tag:
            self._container_depth -= 1
            if self._container_depth == 0:
                self._container_tag = None
                self._heading_parts = None
                self._code_tag = None
                self._code_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._code_tag is not None:
            self._code_parts.append(data)
