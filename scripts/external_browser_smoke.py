#!/usr/bin/env python3
"""Run the opt-in real-browser smoke against an operator-provided URL."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress

from camouflare.browser import make_camoufox_browser_factory
from camouflare.captcha import ClickSolverProvider
from camouflare.config import Settings
from camouflare.models import V1Request
from camouflare.solver import solve_request


async def _run() -> None:
    url = os.environ.get("SMOKE_URL", "").strip()
    if not url:
        raise RuntimeError("SMOKE_URL is required for the external browser smoke.")
    expected = os.environ.get("SMOKE_EXPECT", "").strip()
    settings = Settings(challenge_solver="click")
    browser = await make_camoufox_browser_factory(settings)()
    context = None
    page = None
    try:
        context = await browser.new_context(no_viewport=True)
        page = await context.new_page()
        response = await solve_request(
            V1Request(cmd="request.get", url=url, max_timeout=120_000),
            context=context,
            page=page,
            captcha_provider=ClickSolverProvider(),
            limits=settings.resource_limits,
        )
        if response.status != "ok" or response.solution is None:
            raise RuntimeError("The external browser smoke did not return a solution.")
        if expected and expected not in (response.solution.response or ""):
            raise RuntimeError("The external browser smoke response missed SMOKE_EXPECT.")
    finally:
        if page is not None:
            with suppress(BaseException):
                await page.close()
        if context is not None:
            with suppress(BaseException):
                await context.close()
        with suppress(BaseException):
            await browser.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
