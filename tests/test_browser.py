from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from camouflare import browser
from camouflare.cleanup import CleanupSupervisor
from camouflare.config import Settings


def test_runtime_preflight_rejects_missing_linux_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(browser.platform, "system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="/dev/shm"):
        browser.validate_runtime_environment(tmp_path / "missing-shm")


def test_runtime_preflight_allows_non_linux_without_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")

    browser.validate_runtime_environment(tmp_path / "missing-shm")


def test_runtime_preflight_accepts_writable_linux_shared_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(browser.platform, "system", lambda: "Linux")
    shm_path = tmp_path / "shm"
    shm_path.mkdir()

    browser.validate_runtime_environment(shm_path)


def test_playwright_page_error_patch_defaults_missing_location(tmp_path: Path) -> None:
    core_bundle = tmp_path / "coreBundle.js"
    core_bundle.write_text(
        """
class Page {
      addPageError(error, location2) {
        const pageError = { error, location: location2 };
        this._pageErrors.push(pageError);
      }
}
""",
        encoding="utf-8",
    )

    patcher = getattr(browser, "_patch_core_bundle_page_error_location", None)
    assert patcher is not None

    assert patcher(core_bundle) is True
    patched = core_bundle.read_text(encoding="utf-8")
    assert (
        'const pageError = { error, location: location2 || { url: "", lineNumber: 0, '
        "columnNumber: 0 } };"
    ) in patched
    assert patcher(core_bundle) is False


@pytest.mark.anyio
async def test_virtual_headless_is_rejected_on_non_linux_before_camoufox_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser.platform, "system", lambda: "Darwin")
    fake_module = types.ModuleType("camoufox.async_api")

    class ExplodingCamoufox:
        def __init__(self, **_: object) -> None:
            raise AssertionError("Camoufox should not be constructed for invalid virtual mode")

    fake_module.AsyncCamoufox = ExplodingCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_module)

    factory = browser.make_camoufox_browser_factory(Settings(headless="virtual"))

    with pytest.raises(RuntimeError, match=r"HEADLESS=virtual.*Linux"):
        await factory()


@pytest.mark.anyio
async def test_camoufox_factory_patches_playwright_before_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    fake_module = types.ModuleType("camoufox.async_api")

    class FakeCamoufox:
        def __init__(self, **_: object) -> None:
            calls.append("construct")

        async def __aenter__(self) -> object:
            calls.append("enter")
            return object()

        async def __aexit__(self, *_: object) -> None:
            calls.append("exit")

    fake_module.AsyncCamoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_module)
    monkeypatch.setattr(browser, "validate_runtime_environment", lambda: None)
    monkeypatch.setattr(
        browser,
        "patch_playwright_cancelled_protocol_future",
        lambda: calls.append("protocol-patch"),
        raising=False,
    )
    monkeypatch.setattr(
        browser,
        "patch_playwright_page_error_location",
        lambda: calls.append("patch"),
        raising=False,
    )

    factory = browser.make_camoufox_browser_factory(Settings(challenge_solver="none"))
    handle = await factory()
    await handle.close()

    assert calls == ["protocol-patch", "patch", "construct", "enter", "exit"]


@pytest.mark.anyio
async def test_camoufox_launch_does_not_enable_geoip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options: list[dict[str, object]] = []
    fake_module = types.ModuleType("camoufox.async_api")

    class FakeCamoufox:
        def __init__(self, **kwargs: object) -> None:
            options.append(kwargs)

        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    fake_module.AsyncCamoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_module)
    monkeypatch.setattr(browser, "validate_runtime_environment", lambda: None)
    monkeypatch.setattr(browser, "patch_playwright_cancelled_protocol_future", lambda: None)
    monkeypatch.setattr(browser, "patch_playwright_page_error_location", lambda: None)

    handle = await browser.make_camoufox_browser_factory(Settings())()
    await handle.close()

    assert len(options) == 1
    assert "geoip" not in options[0]


@pytest.mark.anyio
async def test_cancelled_camoufox_launch_closes_partially_entered_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enter_started = asyncio.Event()
    exit_finished = asyncio.Event()
    exit_arguments: list[tuple[object, ...]] = []
    fake_module = types.ModuleType("camoufox.async_api")

    class FakeCamoufox:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> object:
            enter_started.set()
            await asyncio.Event().wait()
            return object()

        async def __aexit__(self, *args: object) -> None:
            exit_arguments.append(args)
            exit_finished.set()

    fake_module.AsyncCamoufox = FakeCamoufox
    monkeypatch.setitem(sys.modules, "camoufox.async_api", fake_module)
    monkeypatch.setattr(browser, "validate_runtime_environment", lambda: None)
    monkeypatch.setattr(browser, "patch_playwright_cancelled_protocol_future", lambda: None)
    monkeypatch.setattr(browser, "patch_playwright_page_error_location", lambda: None)
    cleanup = CleanupSupervisor(timeout_seconds=0.1)
    factory = browser.make_camoufox_browser_factory(
        Settings(cleanup_timeout_seconds=0.1),
        cleanup_supervisor=cleanup,
    )

    launch = asyncio.create_task(factory())
    await enter_started.wait()
    launch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await launch

    assert exit_finished.is_set()
    assert exit_arguments and exit_arguments[0][0] is asyncio.CancelledError
    assert cleanup.snapshot().in_flight == 0
    await cleanup.close()


@pytest.mark.anyio
async def test_browser_close_survives_caller_cancellation() -> None:
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class Manager:
        calls = 0

        async def __aexit__(self, *_: object) -> None:
            self.calls += 1
            close_started.set()
            await allow_close.wait()

    manager = Manager()
    handle = browser.CamoufoxBrowserHandle(manager, object())
    first = asyncio.create_task(handle.close())
    await close_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    allow_close.set()
    await handle.close()
    await handle.close()
    assert manager.calls == 1


@pytest.mark.anyio
async def test_browser_close_failure_can_be_retried() -> None:
    class Manager:
        calls = 0

        async def __aexit__(self, *_: object) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("close failed")

    manager = Manager()
    handle = browser.CamoufoxBrowserHandle(manager, object())

    with pytest.raises(RuntimeError, match="close failed"):
        await handle.close()
    await handle.close()
    assert manager.calls == 2


@pytest.mark.anyio
async def test_playwright_protocol_patch_cancels_callback_future(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from playwright._impl import _connection as connection_module

    original = connection_module.Channel._inner_send
    monkeypatch.setattr(browser, "_PLAYWRIGHT_CANCEL_PATCH_STATUS", "pending")
    monkeypatch.setattr(browser, "_installed_playwright_version", lambda: "1.61.0")
    try:
        assert browser.patch_playwright_cancelled_protocol_future() == "applied"
        loop = asyncio.get_running_loop()
        callback = types.SimpleNamespace(future=loop.create_future())
        fake_connection = types.SimpleNamespace(
            _error=None,
            _transport=types.SimpleNamespace(on_error_future=loop.create_future()),
            _send_message_to_server=lambda *_: callback,
        )
        fake_channel = types.SimpleNamespace(_connection=fake_connection, _object=object())
        task = asyncio.create_task(
            connection_module.Channel._inner_send(fake_channel, "goto", None, None, False)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert callback.future.cancelled()
    finally:
        connection_module.Channel._inner_send = original


def test_playwright_protocol_patch_warns_for_unverified_version(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(browser, "_PLAYWRIGHT_CANCEL_PATCH_STATUS", "pending")
    monkeypatch.setattr(browser, "_installed_playwright_version", lambda: "1.62.0")

    with caplog.at_level("WARNING"):
        status = browser.patch_playwright_cancelled_protocol_future()

    assert status == "unverified"
    assert "not verified" in caplog.text


def test_playwright_protocol_patch_rejects_source_fingerprint_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(browser, "_PLAYWRIGHT_CANCEL_PATCH_STATUS", "pending")
    monkeypatch.setattr(browser, "_installed_playwright_version", lambda: "1.61.0")
    monkeypatch.setattr(browser.inspect, "getsource", lambda _target: "modified source")

    with caplog.at_level("WARNING"):
        status = browser.patch_playwright_cancelled_protocol_future()

    assert status == "fingerprint_mismatch"
    assert "fingerprint was not recognized" in caplog.text
