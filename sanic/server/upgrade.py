from __future__ import annotations

from asyncio import Event
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any, Coroutine


if TYPE_CHECKING:
    from sanic.request import Request
    from sanic.server.protocols.http_protocol import HttpProtocol


UpgradeHandler = Callable[
    ["Request", "UpgradeConnection"], Coroutine[Any, Any, None]
]


class Upgrade:
    """Marker returned from a route handler to accept an HTTP Upgrade.

    When a route handler returns an instance of this class, Sanic sends a
    ``101 Switching Protocols`` response and hands the connection over to
    ``handler``. Any exception raised (or regular HTTP response returned)
    before the ``101`` has been sent is processed by the regular HTTP
    error handling. Afterwards, the connection belongs to ``handler``.

    Args:
        handler (UpgradeHandler): An async callable receiving the
            :class:`~sanic.request.Request` and an
            :class:`UpgradeConnection`. It owns the connection until it
            returns. When it returns (or raises), Sanic closes the
            transport.
        headers (dict[str, str], optional): Extra headers to include in
            the ``101 Switching Protocols`` response.
    """

    __slots__ = ("handler", "headers")

    def __init__(
        self,
        handler: UpgradeHandler,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.handler = handler
        self.headers = headers


class UpgradeConnection:
    """A raw connection taken over from the HTTP layer after a ``101``.

    Bytes that the client already sent after the request headers (for
    example in the same TCP packet as the upgrade request) are preserved
    and returned by the first :meth:`receive` calls.

    The connection is closed when:

    - the handler calls :meth:`close`, or returns (Sanic closes the
      transport on its behalf);
    - the handler raises (the exception is logged and Sanic closes the
      transport);
    - the peer disconnects (pending :meth:`receive` calls return ``b""``
      and the handler task is cancelled);
    - the server stops (the handler task is cancelled and the transport
      is closed).
    """

    __slots__ = (
        "_protocol",
        "_buffer",
        "_data_received",
        "_closed",
        "request",
    )

    def __init__(self, protocol: HttpProtocol, request: Request) -> None:
        self._protocol = protocol
        self.request = request
        self._buffer = bytearray()
        self._data_received = Event()
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterate()

    @property
    def closed(self) -> bool:
        """Whether the connection has been closed."""
        return self._closed

    @property
    def transport(self):
        """The underlying asyncio transport, if still connected."""
        return self._protocol.transport

    @property
    def buffered(self) -> int:
        """Number of bytes received but not yet consumed."""
        return len(self._buffer)

    def feed_data(self, data: bytes) -> None:
        if self._closed:
            return
        self._buffer += data
        self._data_received.set()

    def feed_eof(self) -> None:
        self._closed = True
        self._data_received.set()

    async def receive(self) -> bytes:
        """Wait for and return the next chunk of data.

        Returns ``b""`` once the connection has been closed and no more
        buffered data remains.
        """
        while True:
            if self._buffer:
                data = bytes(self._buffer)
                del self._buffer[:]
                return data
            if self._closed:
                return b""
            transport = self._protocol.transport
            if transport is not None:
                transport.resume_reading()
            self._data_received.clear()
            await self._data_received.wait()

    async def send(self, data: bytes) -> None:
        """Write raw bytes to the connection."""
        if self._closed:
            raise ConnectionError("Cannot send on a closed connection")
        await self._protocol._can_write.wait()
        transport = self._protocol.transport
        if transport is None or transport.is_closing():
            self._closed = True
            raise ConnectionError("Cannot send on a closed connection")
        transport.write(data)

    async def close(self) -> None:
        """Close the connection and release the transport."""
        if self._closed:
            return
        self._closed = True
        self._data_received.set()
        self._protocol.close()

    async def _iterate(self) -> AsyncIterator[bytes]:
        while True:
            data = await self.receive()
            if not data:
                break
            yield data
