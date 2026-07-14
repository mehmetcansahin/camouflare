from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, cast

from camouflare.config import Settings
from camouflare.protocols import BrowserContextLike, BrowserFactory

logger = logging.getLogger(__name__)

_PAGE_ERROR_LOCATION_TARGET = "const pageError = { error, location: location2 };"
_PAGE_ERROR_LOCATION_REPLACEMENT = (
    'const pageError = { error, location: location2 || { url: "", lineNumber: 0, '
    "columnNumber: 0 } };"
)


class CamoufoxBrowserHandle:
    def __init__(self, manager: Any, browser: Any) -> None:
        self._manager = manager
        self._browser = browser
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)

    async def new_context(self, **options: Any) -> BrowserContextLike:
        context = await self._browser.new_context(**options)
        return cast(BrowserContextLike, context)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._manager.__aexit__(None, None, None)


def validate_runtime_environment(shm_path: Path | None = None) -> None:
    if platform.system() != "Linux":
        return
    shm_path = shm_path or Path("/dev/shm")
    if not shm_path.exists() or not os.access(shm_path, os.W_OK):
        raise RuntimeError(
            "Linux browser runtime requires writable /dev/shm. "
            "Run with Docker's default shared memory mount or provide a writable /dev/shm."
        )


def validate_headless_mode(headless: str | bool) -> None:
    if headless == "virtual" and platform.system() != "Linux":
        raise RuntimeError(
            "HEADLESS=virtual is only supported on Linux. Use HEADLESS=true "
            "or HEADLESS=false on macOS and Windows."
        )


def _playwright_core_bundle_path() -> Path:
    import playwright

    package_path = Path(playwright.__file__ or "").resolve().parent
    return package_path / "driver" / "package" / "lib" / "coreBundle.js"


def _patch_core_bundle_page_error_location(bundle_path: Path) -> bool:
    source = bundle_path.read_text(encoding="utf-8")
    if _PAGE_ERROR_LOCATION_REPLACEMENT in source:
        return False
    if _PAGE_ERROR_LOCATION_TARGET not in source:
        raise RuntimeError("Unexpected Playwright coreBundle.js layout.")

    bundle_path.write_text(
        source.replace(_PAGE_ERROR_LOCATION_TARGET, _PAGE_ERROR_LOCATION_REPLACEMENT, 1),
        encoding="utf-8",
    )
    return True


def patch_playwright_page_error_location() -> None:
    """Work around Playwright 1.61 Firefox page errors that omit location."""
    try:
        patched = _patch_core_bundle_page_error_location(_playwright_core_bundle_path())
    except Exception as exc:
        logger.warning("Could not patch Playwright page-error location handling: %s", exc)
        return

    if patched:
        logger.info("Patched Playwright page-error location handling.")


def make_camoufox_browser_factory(settings: Settings) -> BrowserFactory:
    async def factory() -> CamoufoxBrowserHandle:
        validate_headless_mode(settings.headless)
        validate_runtime_environment()
        patch_playwright_page_error_location()

        from camoufox.async_api import AsyncCamoufox

        launch_options: dict[str, Any] = {
            "headless": settings.headless,
            "humanize": True,
            "i_know_what_im_doing": True,
            "main_world_eval": True,
            "config": {"forceScopeAccess": True},
            "disable_coop": True,
        }
        if settings.challenge_solver != "none":
            # The ClickSolver relies on a bundled Camoufox add-on that must be
            # registered at launch (it powers the add_init_script workaround).
            from playwright_captcha.utils.camoufox_add_init_script.add_init_script import (
                get_addon_path,
            )

            launch_options["addons"] = [str(Path(get_addon_path()).absolute())]
        manager = AsyncCamoufox(**launch_options)
        browser = await manager.__aenter__()
        return CamoufoxBrowserHandle(manager, browser)

    return factory
