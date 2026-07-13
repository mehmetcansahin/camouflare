from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from camouflare import browser
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
        "patch_playwright_page_error_location",
        lambda: calls.append("patch"),
        raising=False,
    )

    factory = browser.make_camoufox_browser_factory(Settings(challenge_solver="none"))
    handle = await factory()
    await handle.close()

    assert calls == ["patch", "construct", "enter", "exit"]


@pytest.mark.anyio
async def test_camoufox_geoip_is_offline_by_default_and_explicitly_configurable(
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
    monkeypatch.setattr(browser, "patch_playwright_page_error_location", lambda: None)

    default_handle = await browser.make_camoufox_browser_factory(Settings())()
    enabled_handle = await browser.make_camoufox_browser_factory(Settings(camoufox_geoip=True))()
    await default_handle.close()
    await enabled_handle.close()

    assert [launch["geoip"] for launch in options] == [False, True]
