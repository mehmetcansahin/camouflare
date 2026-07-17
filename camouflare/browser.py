from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import platform
import textwrap
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, cast

from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings
from camouflare.protocols import BrowserContextLike, BrowserFactory

logger = logging.getLogger(__name__)

_PAGE_ERROR_LOCATION_TARGET = "const pageError = { error, location: location2 };"
_PAGE_ERROR_LOCATION_REPLACEMENT = (
    'const pageError = { error, location: location2 || { url: "", lineNumber: 0, '
    "columnNumber: 0 } };"
)
_PLAYWRIGHT_CANCEL_PATCH_VERSION = "1.61.0"
_PLAYWRIGHT_INNER_SEND_FINGERPRINT = (
    "4c6bf1f8b8138acf3cd26579ececb99f790e62a51d6a9013b175d2e4c868563c"
)
_PLAYWRIGHT_CANCEL_PATCH_STATUS = "pending"


class CamoufoxBrowserHandle:
    def __init__(self, manager: Any, browser: Any) -> None:
        self._manager = manager
        self._browser = browser
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser, name)

    async def new_context(self, **options: Any) -> BrowserContextLike:
        context = await self._browser.new_context(**options)
        return cast(BrowserContextLike, context)

    async def close(self) -> None:
        if self._closed:
            return
        await asyncio.shield(self.start_close())

    def start_close(self) -> asyncio.Task[None]:
        """Return the one physical close task shared by every close caller."""

        task = self._close_task
        if task is None:
            task = asyncio.create_task(
                self._manager.__aexit__(None, None, None),
                name="camouflare-browser-close",
            )
            self._close_task = task
            task.add_done_callback(self._browser_close_finished)
        return task

    def _browser_close_finished(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException:
            # A failed close must remain retryable. In particular, do not claim the
            # browser is closed merely because the first caller was cancelled.
            if self._close_task is task:
                self._close_task = None
        else:
            self._closed = True


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


def _installed_playwright_version() -> str | None:
    try:
        return version("playwright")
    except PackageNotFoundError:
        return None


def playwright_cancel_patch_status() -> str:
    """Return the runtime state of the guarded Playwright cancellation workaround."""

    return _PLAYWRIGHT_CANCEL_PATCH_STATUS


def patch_playwright_cancelled_protocol_future() -> str:
    """Cancel Playwright 1.61 protocol futures when their awaiting task is cancelled.

    Playwright 1.61's ``Channel._inner_send`` only cancels the protocol callback
    after ``asyncio.wait`` returns. Cancellation while it is awaiting that call can
    therefore leave the callback alive until browser shutdown, at which point the
    late TargetClosedError is reported as an un-retrieved Future exception. This
    patch is intentionally version- and shape-gated because the target is private.
    """

    global _PLAYWRIGHT_CANCEL_PATCH_STATUS

    if _PLAYWRIGHT_CANCEL_PATCH_STATUS != "pending":
        return _PLAYWRIGHT_CANCEL_PATCH_STATUS

    installed_version = _installed_playwright_version()
    if installed_version is None:
        _PLAYWRIGHT_CANCEL_PATCH_STATUS = "unavailable"
        logger.warning("Playwright is unavailable; cancellation workaround was not applied.")
        return _PLAYWRIGHT_CANCEL_PATCH_STATUS
    if installed_version != _PLAYWRIGHT_CANCEL_PATCH_VERSION:
        _PLAYWRIGHT_CANCEL_PATCH_STATUS = "unverified"
        logger.warning(
            "Playwright %s is not verified for the cancellation workaround; "
            "expected %s and left runtime code unchanged.",
            installed_version,
            _PLAYWRIGHT_CANCEL_PATCH_VERSION,
        )
        return _PLAYWRIGHT_CANCEL_PATCH_STATUS

    try:
        from playwright._impl import _connection as connection_module

        original = connection_module.Channel._inner_send
        if getattr(original, "__camouflare_cancel_patch__", False):
            _PLAYWRIGHT_CANCEL_PATCH_STATUS = "applied"
            return _PLAYWRIGHT_CANCEL_PATCH_STATUS

        parameter_names = tuple(inspect.signature(original).parameters)
        source = textwrap.dedent(inspect.getsource(original)).strip()
        fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
        expected_parameters = (
            "self",
            "method",
            "timeout_calculator",
            "params",
            "return_as_dict",
        )
        if (
            parameter_names != expected_parameters
            or fingerprint != _PLAYWRIGHT_INNER_SEND_FINGERPRINT
        ):
            _PLAYWRIGHT_CANCEL_PATCH_STATUS = "fingerprint_mismatch"
            logger.warning(
                "Playwright %s Channel._inner_send fingerprint was not recognized; "
                "cancellation workaround was not applied.",
                installed_version,
            )
            return _PLAYWRIGHT_CANCEL_PATCH_STATUS

        async def patched_inner_send(
            self: Any,
            method: str,
            timeout_calculator: Any,
            params: dict[str, Any] | None,
            return_as_dict: bool,
        ) -> Any:
            if self._connection._error:
                error = self._connection._error
                self._connection._error = None
                raise error
            callback = self._connection._send_message_to_server(
                self._object,
                method,
                connection_module._augment_params(params, timeout_calculator),
            )
            try:
                done, _ = await asyncio.wait(
                    {
                        self._connection._transport.on_error_future,
                        callback.future,
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )
                result = next(iter(done)).result()
            finally:
                if not callback.future.done():
                    callback.future.cancel()
            if not result:
                return None
            assert isinstance(result, dict)
            if return_as_dict:
                return result
            if len(result) == 0:
                return None
            assert len(result) == 1
            key = next(iter(result))
            return result[key]

        patched_inner_send.__camouflare_cancel_patch__ = True  # type: ignore[attr-defined]
        connection_module.Channel._inner_send = patched_inner_send
    except Exception as exc:
        _PLAYWRIGHT_CANCEL_PATCH_STATUS = "unavailable"
        logger.warning("Could not patch Playwright protocol cancellation handling: %s", exc)
    else:
        _PLAYWRIGHT_CANCEL_PATCH_STATUS = "applied"
        logger.info("Patched Playwright protocol cancellation handling.")
    return _PLAYWRIGHT_CANCEL_PATCH_STATUS


def make_camoufox_browser_factory(
    settings: Settings,
    *,
    cleanup_supervisor: CleanupSupervisor | None = None,
) -> BrowserFactory:
    async def factory() -> CamoufoxBrowserHandle:
        validate_headless_mode(settings.headless)
        validate_runtime_environment()
        patch_playwright_cancelled_protocol_future()
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
        try:
            browser = await manager.__aenter__()
        except BaseException as exc:
            cleanup_awaitable = manager.__aexit__(type(exc), exc, exc.__traceback__)
            if cleanup_supervisor is not None:
                cleanup_task = cleanup_supervisor.start(
                    cleanup_awaitable,
                    kind="browser",
                    timeout_seconds=settings.cleanup_timeout_seconds,
                )
            else:
                cleanup_task = asyncio.ensure_future(cleanup_awaitable)
                cleanup_task.add_done_callback(_consume_background_future)
            # The manager exit is independently owned. Further cancellation of the
            # launch caller cannot cancel or orphan the physical cleanup.
            with suppress(BaseException):
                await asyncio.shield(cleanup_task)
            raise
        return CamoufoxBrowserHandle(manager, browser)

    return factory


def _consume_background_future(future: asyncio.Future[Any]) -> None:
    if future.cancelled():
        return
    with suppress(BaseException):
        future.exception()
