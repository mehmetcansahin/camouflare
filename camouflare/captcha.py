from __future__ import annotations

import logging
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from camouflare.models import V1Request
from camouflare.timer import TimeoutTimer

logger = logging.getLogger(__name__)


class CaptchaProvider(Protocol):
    async def solve(self, *, page: object, request: V1Request, timer: TimeoutTimer) -> str | None:
        """Attempt to solve a challenge and return an optional Turnstile token."""


class NoCaptchaProvider:
    async def solve(self, *, page: object, request: V1Request, timer: TimeoutTimer) -> str | None:
        return None


# Solver bound to a page for the duration of a single request. The provider is a
# shared singleton, so per-request state must not live on ``self``; keying by the
# page (weakly, so a closed page is collected) mirrors solver.py's ``_media_routes``.
_active_solvers: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()


class ClickSolverProvider:
    """Cloudflare interstitial/Turnstile solver backed by playwright-captcha's ClickSolver.

    The ClickSolver must be entered (its ``prepare`` runs the Camoufox add_init_script
    workaround) BEFORE the page is navigated, so ``prepare`` is an async context that
    solve_request enters around the whole navigate+solve span. ClickSolver returns a
    bool, not a token, so ``solve`` returns None (a real Turnstile token requires an
    external API solver, which is intentionally out of scope here).
    """

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        attempt_delay: int = 1,
        wait_checkbox_attempts: int = 3,
        wait_checkbox_delay: float = 1.0,
        solver_factory: Any | None = None,
        captcha_type: Any | None = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._attempt_delay = attempt_delay
        self._wait_checkbox_attempts = wait_checkbox_attempts
        self._wait_checkbox_delay = wait_checkbox_delay
        # Injectable so unit tests can exercise the provider without importing
        # playwright_captcha or launching a real browser.
        self._solver_factory = solver_factory
        self._captcha_type = captcha_type

    def _make_solver(self, page: Any) -> Any:
        if self._solver_factory is not None:
            return self._solver_factory(page)
        from playwright_captcha import ClickSolver, FrameworkType

        return ClickSolver(
            framework=FrameworkType.CAMOUFOX,
            page=page,
            max_attempts=self._max_attempts,
            attempt_delay=self._attempt_delay,
        )

    def _resolve_captcha_type(self) -> Any:
        if self._captcha_type is not None:
            return self._captcha_type
        from playwright_captcha import CaptchaType

        return CaptchaType.CLOUDFLARE_INTERSTITIAL

    @asynccontextmanager
    async def prepare(self, *, page: Any) -> AsyncIterator[None]:
        async with self._make_solver(page) as solver:
            _active_solvers[page] = solver
            try:
                yield None
            finally:
                _active_solvers.pop(page, None)

    async def solve(self, *, page: object, request: V1Request, timer: TimeoutTimer) -> str | None:
        solver = _active_solvers.get(page)
        if solver is None:
            return None
        try:
            await solver.solve_captcha(
                captcha_container=page,
                captcha_type=self._resolve_captcha_type(),
                wait_checkbox_attempts=self._wait_checkbox_attempts,
                wait_checkbox_delay=self._wait_checkbox_delay,
            )
        except Exception as exc:
            # Failing to find/click the widget (common for passive interstitials that
            # clear on their own, or hard-blocked sessions) is NOT fatal: return None
            # so the caller can wait for the challenge to clear instead of erroring.
            logger.info("ClickSolver could not actively solve the challenge: %s", exc)
        # ClickSolver clears the challenge in-page but yields no token.
        return None
