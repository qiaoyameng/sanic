from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from asyncio.transports import Transport

    from sanic.models.server_types import ConnInfo
    from sanic.request import Request
    from sanic.server.protocols.http_protocol import HttpProtocol


class UpgradeConnection:
    """A raw connection handed over to a takeover handler.

    An instance of this class is passed to the handler registered with
    :meth:`Request.accept_upgrade` once Sanic has sent the
    ``101 Switching Protocols`` response. From that moment on, the
    connection no longer speaks HTTP: this object is the only interface
    to the underlying transport, and any bytes the client sent along
    with (or after) the upgrade request are available through
    :meth:`read`.

    **Ownership of the transport**

    - While the takeover handler is running, the handler owns the
      connection. Sanic will not read from it, write to it, or reuse it
      for other requests.
    - When the handler returns, Sanic closes the transport if the
      handler did not already call :meth:`close`.
    - If the handler raises, the exception is logged and Sanic closes
      the transport. No HTTP error response is (or can be) sent.
    - When the server stops, Sanic closes the transport. Pending and
      future :meth:`read` calls then return ``b""`` (EOF), giving the
      handler a chance to finish cleanly.
    - When the client disconnects, a pending :meth:`read` raises
      :class:`asyncio.CancelledError` and subsequent reads return
      ``b""``. Sanic releases the transport.
    """

    def __init__(
        self,
        protocol: HttpProtocol,
        request: Request,
        buffered: bytes = b"",
    ) -> None:
        self._protocol = protocol
        self.request = request
        self._buffer = bytearray(buffered)
        self._waiter: asyncio.Future[None] | None = None
        self._closed_waiter: asyncio.Future[None] = (
            protocol.loop.create_future()
        )
        self._eof = False
        self._closed = False
        self._reading_paused = False

    def __repr__(self) -> str:
        return (
            f"<UpgradeConnection: {self.request.method} {self.request.path}>"
        )

    @property
    def transport(self) -> Transport | None:
        """The underlying asyncio transport, if still available."""
        return self._protocol.transport

    @property
    def conn_info(self) -> ConnInfo | None:
        """Connection information of the underlying connection."""
        return self._protocol.conn_info

    @property
    def closed(self) -> bool:
        """Whether the connection has been closed."""
        return self._closed

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        """Get optional transport information (e.g. ``peername``)."""
        transport = self._protocol.transport
        if transport is None:
            return default
        return transport.get_extra_info(name, default)

    async def read(self, n: int = -1) -> bytes:
        """Read data sent by the client.

        Args:
            n (int): Maximum number of bytes to return. With the default
                of ``-1``, read until EOF and return everything. With a
                positive value, return as soon as at least one byte is
                available (up to ``n`` bytes).

        Returns:
            bytes: The data read. ``b""`` indicates EOF, i.e. the client
                disconnected or the connection is being shut down.
        """
        if n is None or n < 0:
            chunks = []
            while True:
                if self._buffer:
                    chunks.append(self._take(len(self._buffer)))
                elif self._eof or self._closed:
                    return b"".join(chunks)
                else:
                    await self._wait_for_data()
        if n == 0:
            return b""
        await self._wait_for_data()
        return self._take(n)

    def write(self, data: bytes) -> None:
        """Write data to the client.

        Raises:
            ConnectionError: If the connection is (being) closed.
        """
        transport = self._protocol.transport
        if self._closed or transport is None or transport.is_closing():
            raise ConnectionError("Cannot write: the connection is closed")
        transport.write(data)

    async def drain(self) -> None:
        """Wait until the write buffer has been flushed below the
        low-water mark (write backpressure)."""
        await self._protocol._can_write.wait()

    def close(self) -> None:
        """Close the connection.

        The takeover handler should call this when it is done. If it
        does not, Sanic closes the transport once the handler returns.
        """
        if self._closed:
            return
        self._closed = True
        self._wake()
        self._protocol.close()

    async def wait_closed(self) -> None:
        """Wait until the underlying transport has been closed."""
        await self._closed_waiter

    def feed_data(self, data: bytes) -> None:
        """Receive raw bytes from the transport (protocol callback)."""
        self._buffer += data
        self._wake()
        transport = self._protocol.transport
        if (
            transport is not None
            and not self._reading_paused
            and len(self._buffer)
            >= self._protocol.app.config.REQUEST_BUFFER_SIZE
        ):
            transport.pause_reading()
            self._reading_paused = True

    def feed_eof(self) -> None:
        """Signal that the client half-closed the connection."""
        self._eof = True
        self._wake()

    def _connection_lost(self) -> None:
        """Signal that the transport has been closed."""
        self._eof = True
        self._closed = True
        self._wake()
        if not self._closed_waiter.done():
            self._closed_waiter.set_result(None)

    def _take(self, n: int) -> bytes:
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        transport = self._protocol.transport
        if (
            self._reading_paused
            and transport is not None
            and len(self._buffer)
            < self._protocol.app.config.REQUEST_BUFFER_SIZE // 2
        ):
            transport.resume_reading()
            self._reading_paused = False
        return data

    async def _wait_for_data(self) -> None:
        if self._buffer or self._eof or self._closed:
            return
        waiter: asyncio.Future[None] = self._protocol.loop.create_future()
        self._waiter = waiter
        try:
            await waiter
        finally:
            if self._waiter is waiter:
                self._waiter = None

    def _wake(self) -> None:
        waiter, self._waiter = self._waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)
