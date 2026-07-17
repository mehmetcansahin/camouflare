from __future__ import annotations

import hashlib
import json
import platform
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, urlsplit

from camouflare.browser import (
    CamoufoxBrowserHandle,
    patch_playwright_cancelled_protocol_future,
    patch_playwright_page_error_location,
)
from camouflare.config import Settings


class _FixtureServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureRequestHandler)
        self._request_count = 0
        self._request_count_lock = threading.Lock()

    @property
    def request_count(self) -> int:
        with self._request_count_lock:
            return self._request_count

    def record_request(self) -> None:
        with self._request_count_lock:
            self._request_count += 1


class _FixtureRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CamouflareFixture/1.0"
    sys_version = ""

    _HTML_PREFIX: ClassVar[str] = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Local integration fixture</title></head><body>"
    )
    _HTML_SUFFIX: ClassVar[str] = "</body></html>"

    def log_message(self, format: str, *args: Any) -> None:
        # Browser requests (including favicon probes) would otherwise make CI logs noisy.
        return

    def do_GET(self) -> None:
        self._fixture_server.record_request()
        parsed = urlsplit(self.path)

        if parsed.path in {"/", "/health"}:
            self._send_html('<main id="fixture-health">camouflare-integration-fixture</main>')
            return
        if parsed.path == "/get":
            self._send_html('<main id="get-result">get-ok</main>')
            return
        if parsed.path == "/redirect":
            self._send_redirect("/final?from=redirect")
            return
        if parsed.path == "/final":
            self._send_html('<main id="redirect-result">redirect-ok</main>')
            return
        if parsed.path == "/delayed":
            self._send_html(
                """
<main id="delayed-result">pending</main>
<script>
  setTimeout(() => {
    document.getElementById("delayed-result").textContent = "javascript-ready";
  }, 150);
</script>
"""
            )
            return
        if parsed.path == "/screenshot":
            self._send_html(
                """
<style>
  html, body { margin: 0; width: 100%; min-height: 100%; background: #17324d; }
  main { color: #f5d76e; font: 700 48px sans-serif; padding: 80px; }
</style>
<main id="screenshot-result">screenshot-ok</main>
"""
            )
            return
        if parsed.path == "/cookies/set":
            value = parse_qs(parsed.query).get("value", ["fixture"])[0]
            if not value.replace("-", "").replace("_", "").isalnum():
                self._send_html('<main id="cookie-error">invalid-cookie-value</main>', status=400)
                return
            self._send_html(
                f'<main id="cookie-set">{escape(value)}</main>',
                headers={"Set-Cookie": f"camouflare_fixture={value}; Path=/; SameSite=Lax"},
            )
            return
        if parsed.path == "/cookies/read":
            cookie = escape(self.headers.get("Cookie", ""))
            self._send_html(f'<pre id="cookie-header">{cookie}</pre>')
            return
        if parsed.path == "/slow":
            raw_delay = parse_qs(parsed.query).get("seconds", ["2"])[0]
            try:
                delay = min(max(float(raw_delay), 0.0), 10.0)
            except ValueError:
                delay = 2.0
            time.sleep(delay)
            self._send_html('<main id="slow-result">slow-ok</main>')
            return

        self._send_html('<main id="not-found">not-found</main>', status=404)

    def do_POST(self) -> None:
        self._fixture_server.record_request()
        parsed = urlsplit(self.path)
        body = self.rfile.read(self._content_length)

        if parsed.path == "/post/form":
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
            serialized = escape(json.dumps(values, sort_keys=True, separators=(",", ":")))
            self._send_html(f'<pre id="form-values">{serialized}</pre>')
            return
        if parsed.path == "/post/json":
            digest = hashlib.sha256(body).hexdigest()
            content_type = escape(self.headers.get("Content-Type", ""), quote=True)
            raw_body = escape(body.decode("utf-8"), quote=False)
            self._send_html(
                f'<div id="json-result" data-body-sha256="{digest}" '
                f'data-content-type="{content_type}">json-ok</div>'
                f'<pre id="json-body">{raw_body}</pre>'
            )
            return

        self._send_html('<main id="not-found">not-found</main>', status=404)

    @property
    def _fixture_server(self) -> _FixtureServer:
        return cast(_FixtureServer, self.server)

    @property
    def _content_length(self) -> int:
        try:
            return max(int(self.headers.get("Content-Length", "0")), 0)
        except ValueError:
            return 0

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send_html(
        self,
        body: str,
        *,
        status: int = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = f"{self._HTML_PREFIX}{body}{self._HTML_SUFFIX}".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(payload)


@dataclass
class LocalHttpServer:
    """A deterministic loopback-only target for real-browser and soak tests."""

    _server: _FixtureServer
    _thread: threading.Thread

    @classmethod
    def start(cls) -> LocalHttpServer:
        server = _FixtureServer()
        thread = threading.Thread(
            target=server.serve_forever,
            name="camouflare-integration-http",
            daemon=True,
        )
        thread.start()
        return cls(server, thread)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def request_count(self) -> int:
        return self._server.request_count

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> LocalHttpServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


BrowserFactory = Callable[[], Awaitable[Any]]


def make_offline_camoufox_factory() -> BrowserFactory:
    """Return a real Camoufox factory that never performs GeoIP network discovery."""

    async def factory() -> CamoufoxBrowserHandle:
        patch_playwright_cancelled_protocol_future()
        patch_playwright_page_error_location()
        from camoufox.async_api import AsyncCamoufox

        target_os = "macos" if platform.system() == "Darwin" else "linux"
        manager = AsyncCamoufox(
            os=target_os,
            headless=True,
            humanize=False,
            i_know_what_im_doing=True,
            main_world_eval=True,
            config={"forceScopeAccess": True},
            disable_coop=True,
        )
        try:
            browser = await manager.__aenter__()
        except BaseException as exc:
            with suppress(BaseException):
                await manager.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return CamoufoxBrowserHandle(manager, browser)

    return factory


def make_browser_test_settings() -> Settings:
    """Settings sized for two isolated sessions plus one transient request."""

    return Settings(
        host="127.0.0.1",
        port=8191,
        camouflare_api_token=None,
        headless=True,
        proxy_url=None,
        proxy_username=None,
        proxy_password=None,
        pool_min_browsers=1,
        pool_max_browsers=1,
        pool_max_contexts_per_browser=3,
        pool_reserved_transient_contexts=1,
        pool_acquire_timeout_ms=15_000,
        max_sessions=2,
        session_ttl_minutes=60,
        browser_max_uses=10_000,
        browser_max_age_minutes=120,
        prometheus_enabled=False,
        challenge_solver="none",
    )
