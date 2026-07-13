from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from camouflare.browser import make_camoufox_browser_factory
from camouflare.captcha import ClickSolverProvider, NoCaptchaProvider
from camouflare.config import Settings
from camouflare.models import V1Request
from camouflare.solver import solve_request


def _smoke_url() -> str:
    url = os.getenv("SMOKE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "SMOKE_URL is required. Point it at a controlled HTTP(S) fixture; "
            "the smoke test has no external default target."
        )
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise RuntimeError("SMOKE_URL must use http or https.")
    return url


def _smoke_timeout_ms() -> int:
    raw_value = os.getenv("SMOKE_MAX_TIMEOUT_MS", "60000")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("SMOKE_MAX_TIMEOUT_MS must be an integer.") from exc
    if value <= 0:
        raise RuntimeError("SMOKE_MAX_TIMEOUT_MS must be greater than zero.")
    return value


async def _smoke(*, url: str, max_timeout_ms: int, expected_text: str | None = None) -> None:
    settings = Settings()
    factory = make_camoufox_browser_factory(settings)
    browser = await factory()
    # Exercise the real solver path (Camoufox add-on + ClickSolver prepare/cleanup)
    # when the solver is enabled, so this manual smoke covers challenge solving too.
    provider = ClickSolverProvider() if settings.challenge_solver != "none" else NoCaptchaProvider()
    try:
        context = await browser.new_context(no_viewport=True)
        try:
            page = await context.new_page()
            try:
                response = await solve_request(
                    V1Request(cmd="request.get", url=url, max_timeout=max_timeout_ms),
                    context=context,
                    page=page,
                    captcha_provider=provider,
                )
                if response.status != "ok" or response.solution is None:
                    raise RuntimeError(
                        f"Unexpected smoke response: {response.status} {response.message}"
                    )
                if urlsplit(response.solution.url).scheme.lower() not in {"http", "https"}:
                    raise RuntimeError(f"Unexpected smoke solution url: {response.solution.url!r}")
                if expected_text:
                    searchable = f"{response.solution.url}\n{response.solution.response or ''}"
                    if expected_text not in searchable:
                        raise RuntimeError(
                            f"SMOKE_EXPECT_TEXT value {expected_text!r} was not found in "
                            "the final URL or response."
                        )
            finally:
                await page.close()
        finally:
            await context.close()
    finally:
        await browser.close()


def main() -> None:
    asyncio.run(
        _smoke(
            url=_smoke_url(),
            max_timeout_ms=_smoke_timeout_ms(),
            expected_text=os.getenv("SMOKE_EXPECT_TEXT") or None,
        )
    )


if __name__ == "__main__":
    main()
