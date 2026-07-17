from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

import pytest

import camouflare.proxy as proxy_module
from camouflare.proxy import (
    LocalSocksBridge,
    _encode_socks_address,
    _read_socks_address,
    _upstream_endpoint,
)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with suppress(BrokenPipeError, ConnectionError, OSError):
        await writer.wait_closed()


@pytest.mark.anyio
async def test_local_socks_bridge_authenticates_to_upstream_socks5() -> None:
    events: list[tuple[str, Any]] = []

    async def handle_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        greeting = await reader.readexactly(3)
        events.append(("greeting", greeting))
        writer.write(b"\x05\x02")
        await writer.drain()

        auth_header = await reader.readexactly(2)
        username = await reader.readexactly(auth_header[1])
        password_length = await reader.readexactly(1)
        password = await reader.readexactly(password_length[0])
        events.append(("auth", username.decode(), password.decode()))
        writer.write(b"\x01\x00")
        await writer.drain()

        request_header = await reader.readexactly(4)
        assert request_header == b"\x05\x01\x00\x03"
        domain_length = await reader.readexactly(1)
        domain = await reader.readexactly(domain_length[0])
        port = int.from_bytes(await reader.readexactly(2), "big")
        events.append(("connect", domain.decode(), port))
        writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    bridge = await LocalSocksBridge.start(
        {
            "server": f"socks5://127.0.0.1:{upstream_port}",
            "username": "ondokuzon",
            "password": "on9on",
        }
    )

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.local_port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"

        domain = b"example.com"
        writer.write(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + (443).to_bytes(2, "big"))
        await writer.drain()
        assert await reader.readexactly(10) == b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        writer.close()
        await writer.wait_closed()
    finally:
        await bridge.close()
        upstream.close()
        await upstream.wait_closed()

    assert events == [
        ("greeting", b"\x05\x01\x02"),
        ("auth", "ondokuzon", "on9on"),
        ("connect", "example.com", 443),
    ]


@pytest.mark.anyio
async def test_local_socks_bridge_close_closes_half_open_clients() -> None:
    bridge = await LocalSocksBridge.start(
        {
            "server": "socks5://127.0.0.1:1",
            "username": "ondokuzon",
            "password": "on9on",
        }
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.local_port)

    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"

        await asyncio.wait_for(bridge.close(), timeout=1.0)
        assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
    finally:
        await _close_writer(writer)
        await bridge.close()


@pytest.mark.anyio
async def test_local_socks_bridge_close_closes_active_tunnel_clients() -> None:
    async def handle_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x02"
            writer.write(b"\x05\x02")
            await writer.drain()

            auth_header = await reader.readexactly(2)
            await reader.readexactly(auth_header[1])
            password_length = await reader.readexactly(1)
            await reader.readexactly(password_length[0])
            writer.write(b"\x01\x00")
            await writer.drain()

            assert await reader.readexactly(4) == b"\x05\x01\x00\x03"
            domain_length = await reader.readexactly(1)
            await reader.readexactly(domain_length[0])
            await reader.readexactly(2)
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()

            await reader.read()
        finally:
            await _close_writer(writer)

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    bridge = await LocalSocksBridge.start(
        {
            "server": f"socks5://127.0.0.1:{upstream_port}",
            "username": "ondokuzon",
            "password": "on9on",
        }
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", bridge.local_port)

    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"

        domain = b"example.com"
        writer.write(b"\x05\x01\x00\x03" + bytes([len(domain)]) + domain + (443).to_bytes(2, "big"))
        await writer.drain()
        assert await reader.readexactly(10) == b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"

        await asyncio.wait_for(bridge.close(), timeout=1.0)
        assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
    finally:
        await _close_writer(writer)
        await bridge.close()
        upstream.close()
        await upstream.wait_closed()


def test_encode_socks_address_covers_ipv4_ipv6_and_domain() -> None:
    assert _encode_socks_address(1, "1.2.3.4") == b"\x01\x01\x02\x03\x04"
    ipv6 = _encode_socks_address(4, "::1")
    assert ipv6[0] == 4
    assert ipv6[1:] == b"\x00" * 15 + b"\x01"
    domain = _encode_socks_address(3, "example.com")
    assert domain == b"\x03" + bytes([len("example.com")]) + b"example.com"


def test_encode_socks_address_rejects_unknown_type_and_long_domain() -> None:
    with pytest.raises(RuntimeError, match="Unsupported SOCKS address type"):
        _encode_socks_address(9, "whatever")
    # Many short labels: each is valid IDNA, but the whole name exceeds 255 bytes.
    with pytest.raises(RuntimeError, match="too long"):
        _encode_socks_address(3, ".".join(["ab"] * 100))


@pytest.mark.anyio
async def test_read_socks_address_decodes_each_address_type() -> None:
    async def read(atyp: int, payload: bytes) -> str:
        reader = asyncio.StreamReader()
        reader.feed_data(payload)
        reader.feed_eof()
        return await _read_socks_address(reader, atyp)

    assert await read(1, b"\x7f\x00\x00\x01") == "127.0.0.1"
    assert await read(4, b"\x00" * 15 + b"\x01") == "::1"
    assert await read(3, bytes([len("example.com")]) + b"example.com") == "example.com"


@pytest.mark.anyio
async def test_read_socks_address_rejects_unknown_type() -> None:
    reader = asyncio.StreamReader()
    reader.feed_eof()
    with pytest.raises(RuntimeError, match="Unsupported SOCKS address type"):
        await _read_socks_address(reader, 9)


def test_upstream_endpoint_requires_host_and_port() -> None:
    assert _upstream_endpoint({"server": "socks5://10.0.0.1:1080"}) == ("10.0.0.1", 1080)
    with pytest.raises(RuntimeError, match="host and port"):
        _upstream_endpoint({"server": "socks5://10.0.0.1"})


@pytest.mark.anyio
async def test_connect_upstream_closes_writer_when_acquisition_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.closed = False
            self.waited = False

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            self.waited = True

    writer = Writer()

    async def open_connection(_host: str, _port: int) -> tuple[asyncio.StreamReader, Writer]:
        return asyncio.StreamReader(), writer

    async def cancel_authentication(*_args: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    monkeypatch.setattr(proxy_module, "_authenticate_upstream", cancel_authentication)

    bridge = LocalSocksBridge(
        server=None,  # type: ignore[arg-type]
        upstream_proxy={
            "server": "socks5://127.0.0.1:1080",
            "username": "user",
            "password": "secret",
        },
        local_host="127.0.0.1",
        local_port=0,
    )
    with pytest.raises(asyncio.CancelledError):
        await bridge._connect_upstream(3, "example.com", 443)

    assert writer.closed is True
    assert writer.waited is True


@pytest.mark.anyio
async def test_handler_done_callback_retrieves_and_logs_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail() -> None:
        raise RuntimeError("handler exploded")

    class Server:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    bridge = LocalSocksBridge(
        server=Server(),  # type: ignore[arg-type]
        upstream_proxy={
            "server": "socks5://127.0.0.1:1",
            "username": "user",
            "password": "secret",
        },
        local_host="127.0.0.1",
        local_port=0,
    )
    try:
        task = asyncio.create_task(fail())
        bridge._handler_tasks.add(task)
        task.add_done_callback(bridge._handler_done)
        with caplog.at_level(logging.ERROR, logger="camouflare.proxy"):
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert task not in bridge._handler_tasks
        assert "SOCKS bridge client handler failed" in caplog.text
        assert "handler exploded" in caplog.text
    finally:
        await bridge.close()
