from __future__ import annotations

import asyncio
import ipaddress
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class LocalSocksBridge:
    """No-auth SOCKS5 relay that adds credentials to an authenticated upstream.

    Playwright/Camoufox only accept an unauthenticated proxy, so this bridge does
    the username/password handshake with the upstream on the browser's behalf. It
    binds to 127.0.0.1 on an ephemeral port and lives only for the request/session
    that owns it. Because the local end is unauthenticated, any other process on
    the same host could use it (with the upstream credentials) while it is open;
    the deployment is assumed to be a single-tenant, loopback-only trust boundary
    (e.g. one container). Do not bind it to a non-loopback interface.
    """

    server: asyncio.AbstractServer
    upstream_proxy: dict[str, str]
    local_host: str
    local_port: int
    _client_writers: set[asyncio.StreamWriter] = field(default_factory=set, init=False)
    _handler_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _closed: bool = field(default=False, init=False)

    @classmethod
    async def start(
        cls,
        upstream_proxy: dict[str, str],
        *,
        local_host: str = "127.0.0.1",
    ) -> LocalSocksBridge:
        bridge = cls(
            server=None,  # type: ignore[arg-type]
            upstream_proxy=upstream_proxy,
            local_host=local_host,
            local_port=0,
        )
        server = await asyncio.start_server(bridge._accept_client, local_host, 0)
        bridge.server = server
        bridge.local_port = int(server.sockets[0].getsockname()[1])
        return bridge

    @property
    def browser_proxy(self) -> dict[str, str]:
        return {"server": f"socks5://{self.local_host}:{self.local_port}"}

    async def close(self) -> None:
        self._closed = True
        self.server.close()
        close_clients = getattr(self.server, "close_clients", None)
        if callable(close_clients):
            close_clients()
        self._close_tracked_clients()
        for task in list(self._handler_tasks):
            task.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)
        await self.server.wait_closed()

    def _accept_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if self._closed:
            client_writer.close()
            return
        self._client_writers.add(client_writer)
        task = asyncio.create_task(self._handle_client(client_reader, client_writer))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def _close_tracked_clients(self) -> None:
        for writer in list(self._client_writers):
            writer.close()

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        connected = False
        try:
            atyp, address, port = await self._read_client_request(client_reader, client_writer)
            upstream_reader, upstream_writer = await self._connect_upstream(atyp, address, port)
            client_writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await client_writer.drain()
            connected = True
            await self._pipe_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )
        except Exception:
            # A SOCKS failure reply is only valid during the handshake. Once the
            # CONNECT success reply has been sent the stream carries tunnel data, so
            # injecting a reply here would corrupt the browser's live connection.
            if not connected and not client_writer.is_closing():
                with suppress(BrokenPipeError, ConnectionError, OSError):
                    client_writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                    await client_writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(BrokenPipeError, ConnectionError, OSError):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with suppress(BrokenPipeError, ConnectionError, OSError):
                await client_writer.wait_closed()
            self._client_writers.discard(client_writer)

    async def _read_client_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> tuple[int, str, int]:
        version, method_count = await reader.readexactly(2)
        if version != 5:
            raise RuntimeError("Unsupported SOCKS version.")
        methods = await reader.readexactly(method_count)
        if 0 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            raise RuntimeError("SOCKS client did not offer no-auth.")
        writer.write(b"\x05\x00")
        await writer.drain()

        version, command, _reserved, atyp = await reader.readexactly(4)
        if version != 5 or command != 1:
            raise RuntimeError("Only SOCKS5 CONNECT is supported.")
        address = await _read_socks_address(reader, atyp)
        port = int.from_bytes(await reader.readexactly(2), "big")
        return atyp, address, port

    async def _connect_upstream(
        self,
        atyp: int,
        address: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream_host, upstream_port = _upstream_endpoint(self.upstream_proxy)
        reader, writer = await asyncio.open_connection(upstream_host, upstream_port)
        try:
            await _authenticate_upstream(reader, writer, self.upstream_proxy)
            writer.write(
                b"\x05\x01\x00" + _encode_socks_address(atyp, address) + port.to_bytes(2, "big")
            )
            await writer.drain()
            version, reply, _reserved, reply_atyp = await reader.readexactly(4)
            if version != 5 or reply != 0:
                raise RuntimeError(f"Upstream SOCKS CONNECT failed with reply {reply}.")
            await _read_socks_address(reader, reply_atyp)
            await reader.readexactly(2)
            return reader, writer
        except Exception:
            writer.close()
            with suppress(BrokenPipeError, ConnectionError, OSError):
                await writer.wait_closed()
            raise

    async def _pipe_bidirectional(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        tasks = {
            asyncio.create_task(pipe(client_reader, upstream_writer)),
            asyncio.create_task(pipe(upstream_reader, client_writer)),
        }
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    raise asyncio.CancelledError
                exc = task.exception()
                if exc is not None:
                    # One direction errored: tear down the peer and surface the failure.
                    for pending_task in pending:
                        pending_task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    raise exc
            # A clean EOF (e.g. the client half-closed after sending its request) must
            # not truncate the other direction: keep relaying until it finishes too.
            if pending:
                remaining, _ = await asyncio.wait(pending)
                for task in remaining:
                    if task.cancelled():
                        raise asyncio.CancelledError
                    exc = task.exception()
                    if exc is not None:
                        raise exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class ProxyLease:
    configured_proxy: dict[str, str] | None
    browser_proxy: dict[str, str] | None
    bridge: LocalSocksBridge | None = None

    async def close(self) -> None:
        if self.bridge is not None:
            await self.bridge.close()


async def open_proxy_lease(configured_proxy: dict[str, str] | None) -> ProxyLease:
    if not _requires_local_bridge(configured_proxy):
        return ProxyLease(configured_proxy=configured_proxy, browser_proxy=configured_proxy)
    assert configured_proxy is not None
    bridge = await LocalSocksBridge.start(configured_proxy)
    return ProxyLease(
        configured_proxy=configured_proxy,
        browser_proxy=bridge.browser_proxy,
        bridge=bridge,
    )


def _requires_local_bridge(proxy: dict[str, str] | None) -> bool:
    if not proxy or not (proxy.get("username") or proxy.get("password")):
        return False
    scheme = urlsplit(proxy["server"]).scheme.lower()
    return scheme in {"socks5", "socks5h"}


def _upstream_endpoint(proxy: dict[str, str]) -> tuple[str, int]:
    parsed = urlsplit(proxy["server"])
    if parsed.hostname is None or parsed.port is None:
        raise RuntimeError("SOCKS proxy server must include host and port.")
    return parsed.hostname, parsed.port


async def _authenticate_upstream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proxy: dict[str, str],
) -> None:
    username = proxy.get("username", "").encode()
    password = proxy.get("password", "").encode()
    if len(username) > 255 or len(password) > 255:
        raise RuntimeError("SOCKS proxy username and password must be at most 255 bytes.")

    writer.write(b"\x05\x01\x02")
    await writer.drain()
    version, method = await reader.readexactly(2)
    if version != 5 or method != 2:
        raise RuntimeError("Upstream SOCKS proxy did not accept username/password auth.")

    writer.write(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
    await writer.drain()
    auth_version, status = await reader.readexactly(2)
    if auth_version != 1 or status != 0:
        raise RuntimeError("Upstream SOCKS proxy authentication failed.")


async def _read_socks_address(reader: asyncio.StreamReader, atyp: int) -> str:
    if atyp == 1:
        return str(ipaddress.IPv4Address(await reader.readexactly(4)))
    if atyp == 3:
        length = (await reader.readexactly(1))[0]
        # Relay the domain bytes verbatim (latin-1 is a 1:1 byte<->str mapping);
        # decoding through idna here would not round-trip on re-encode.
        return (await reader.readexactly(length)).decode("latin-1")
    if atyp == 4:
        return str(ipaddress.IPv6Address(await reader.readexactly(16)))
    raise RuntimeError(f"Unsupported SOCKS address type {atyp}.")


def _encode_socks_address(atyp: int, address: str) -> bytes:
    if atyp == 1:
        return bytes([atyp]) + ipaddress.IPv4Address(address).packed
    if atyp == 3:
        # Symmetric with _read_socks_address: relay the same bytes we received.
        encoded = address.encode("latin-1")
        if len(encoded) > 255:
            raise RuntimeError("SOCKS domain address is too long.")
        return bytes([atyp, len(encoded)]) + encoded
    if atyp == 4:
        return bytes([atyp]) + ipaddress.IPv6Address(address).packed
    raise RuntimeError(f"Unsupported SOCKS address type {atyp}.")
