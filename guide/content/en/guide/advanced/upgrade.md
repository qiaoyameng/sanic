# HTTP Upgrade

Sanic can hand a connection over to a custom protocol through an
[HTTP Upgrade](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Protocol_upgrade_mechanism)
handshake. This is useful for protocols that are **not** WebSocket (for
example an internal binary protocol) but should share the same listening
port as the HTTP application.

## Accepting an upgrade

.. column::

    A route handler that wants to accept an upgrade returns an `Upgrade`
    object wrapping an async handler. Sanic sends a
    `101 Switching Protocols` response and then calls the handler with the
    request and an `UpgradeConnection`.

    Until the `101` response has been sent, regular HTTP error handling
    applies: the route may also return a normal HTTP response to reject
    the upgrade, and any exception raised is turned into an HTTP error
    response.

.. column::

    ```python
    from sanic import Request, Upgrade, UpgradeConnection
    from sanic.response import text

    async def echo_protocol(
        request: Request, connection: UpgradeConnection
    ):
        async for data in connection:
            await connection.send(data)

    @app.get("/echo")
    async def echo(request: Request):
        if request.headers.get("upgrade") != "echo":
            return text("expected upgrade", status=400)
        return Upgrade(echo_protocol)
    ```

## The connection

.. column::

    The `UpgradeConnection` gives raw access to the socket:

    - `await connection.receive()` returns the next chunk of bytes, and
      `b""` once the connection is closed. Bytes the client already sent
      after the request headers (for example in the same TCP packet) are
      preserved and returned first.
    - `await connection.send(data)` writes raw bytes.
    - `await connection.close()` closes the connection.
    - Iterating the connection (`async for data in connection`) yields
      chunks until the connection closes.

.. column::

    ```python
    async def handler(request, connection):
        while data := await connection.receive():
            await connection.send(data.upper())
        await connection.close()
    ```

## Connection lifetime

.. column::

    Ownership of the transport is well defined:

    - Before the `101` response is sent, the HTTP layer owns the
      connection and handles all errors.
    - Once upgraded, the handler owns the connection. When it returns,
      Sanic closes the transport if the handler did not.
    - If the handler raises, the exception is logged and Sanic closes
      the transport. No HTTP error response is sent after a `101`.
    - If the client disconnects, pending `receive()` calls return `b""`
      and the handler task is cancelled.
    - When the server stops, the handler task is cancelled and the
      transport is closed.
