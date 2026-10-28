"""In-memory ``HttpConnector`` for tests of the shared HTTP transport.

A ``FakeConnector`` answers each hop with the next scripted ``FakeHttpResponse`` (or
a callable's answer) and records what the transport sent, so redirect, cookie,
header, decoding, and limit behavior can be asserted without a network.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from camouflare.http_transport import HopRequest, HopResponse, HopSender, HttpTransport

_DEFAULT_CHUNK_BYTES = 65_536


@dataclass
class FakeHttpResponse:
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list)
    # Bytes are delivered in 64 KiB chunks; an iterable is delivered as given.
    body: bytes | Iterable[bytes] = b""
    # Raised instead of answering, e.g. ``httpcore.ConnectError("refused")``.
    error: BaseException | None = None
    # Marks the hop as having reached the target through its proxy before answering
    # or raising, like httpcore's completed SOCKS handshake or CONNECT tunnel.
    proxy_tunnel_open: bool = False


@dataclass(frozen=True)
class RecordedHop:
    method: str
    url: str
    # Lower-cased header names; the transport sends at most one value per name.
    headers: dict[str, str]
    timeout_seconds: float
    proxy: Mapping[str, str] | None
    block_private_targets: bool


class FakeConnector:
    def __init__(
        self,
        responses: Iterable[FakeHttpResponse] | Callable[[HopRequest], FakeHttpResponse],
    ) -> None:
        if callable(responses):
            self._answer = responses
        else:
            scripted = iter(responses)
            self._answer = lambda hop: next(scripted)
        self.hops: list[RecordedHop] = []
        self.sessions: list[tuple[Mapping[str, str] | None, bool]] = []
        # Body bytes the transport actually pulled from each response, in hop order.
        self.consumed_bytes: list[int] = []
        self.closed = False

    @asynccontextmanager
    async def session(
        self,
        *,
        proxy: Mapping[str, str] | None,
        block_private_targets: bool,
    ) -> AsyncGenerator[HopSender]:
        self.sessions.append((proxy, block_private_targets))

        @asynccontextmanager
        async def send(hop: HopRequest) -> AsyncGenerator[HopResponse]:
            self.hops.append(
                RecordedHop(
                    method=hop.method,
                    url=hop.url,
                    headers={name.lower(): value for name, value in hop.headers},
                    timeout_seconds=hop.timeout_seconds,
                    proxy=proxy,
                    block_private_targets=block_private_targets,
                )
            )
            response = self._answer(hop)
            if response.proxy_tunnel_open:
                hop.progress.proxy_tunnel_open = True
            if response.error is not None:
                raise response.error
            index = len(self.consumed_bytes)
            self.consumed_bytes.append(0)

            async def chunks() -> AsyncGenerator[bytes]:
                body = response.body
                parts = (
                    [
                        body[offset : offset + _DEFAULT_CHUNK_BYTES]
                        for offset in range(0, len(body), _DEFAULT_CHUNK_BYTES)
                    ]
                    if isinstance(body, bytes)
                    else list(body)
                )
                for part in parts:
                    self.consumed_bytes[index] += len(part)
                    yield part

            yield HopResponse(status=response.status, headers=response.headers, chunks=chunks())

        yield send

    async def aclose(self) -> None:
        self.closed = True


def fake_transport(
    responses: Iterable[FakeHttpResponse] | Callable[[HopRequest], FakeHttpResponse],
    *,
    max_concurrency: int = 4,
) -> tuple[HttpTransport, FakeConnector]:
    connector = FakeConnector(responses)
    return HttpTransport(max_concurrency=max_concurrency, connector=connector), connector
