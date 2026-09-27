import asyncio

from sanic.response import text
from sanic.server.upgrade import UpgradeConnection


UPGRADE_REQUEST = (
    b"GET /echo HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"Connection: upgrade\r\n"
    b"Upgrade: echo\r\n"
    b"\r\n"
)


async def read_head(reader):
    """Read an HTTP response head, returning (head, leftover bytes)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = await reader.read(1024)
        if not chunk:
            raise ConnectionError("Connection closed during handshake")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def make_echo_handler(results):
    async def echo(connection: UpgradeConnection):
        try:
            while True:
                data = await connection.read(4096)
                if not data:
                    break
                connection.write(data)
                await connection.drain()
        finally:
            connection.close()
            results["handler_done"] = True

    return echo


def add_echo_route(app, results):
    @app.get("/echo")
    async def echo_route(request):
        if request.headers.get("upgrade") != "echo":
            return text("Upgrade required", status=426)
        return request.accept_upgrade(make_echo_handler(results))


def test_upgrade_echo(app, port):
    results = {}
    add_echo_route(app, results)

    @app.listener("after_server_start")
    async def client(app, loop):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(UPGRADE_REQUEST)
            await writer.drain()
            head, _ = await read_head(reader)
            results["head"] = head

            writer.write(b"hello")
            await writer.drain()
            results["echo"] = await reader.readexactly(5)

            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.1)
        finally:
            app.stop()

    app.run(single_process=True, port=port)

    assert results["head"].startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"upgrade: echo" in results["head"].lower()
    assert results["echo"] == b"hello"
    assert results["handler_done"] is True


def test_upgrade_early_data_same_packet(app, port):
    """Binary data sent in the same TCP packet as the request headers
    must be handed over to the takeover handler, not lost."""
    results = {}
    add_echo_route(app, results)
    early = b"\x00\x01\x02\xffbinary"

    @app.listener("after_server_start")
    async def client(app, loop):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # Headers and protocol data in a single write (one packet)
            writer.write(UPGRADE_REQUEST + early)
            await writer.drain()
            head, rest = await read_head(reader)
            results["head"] = head

            leftover = len(early) - len(rest)
            results["echo"] = rest + await reader.readexactly(leftover)

            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.1)
        finally:
            app.stop()

    app.run(single_process=True, port=port)

    assert results["head"].startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert results["echo"] == early
    assert results["handler_done"] is True


def test_upgrade_rejected_keeps_http_error_handling(app, port):
    results = {}

    @app.get("/echo")
    async def echo_route(request):
        if request.headers.get("upgrade") != "echo":
            return text("Upgrade required", status=426)
        return request.accept_upgrade(make_echo_handler(results))

    @app.get("/boom")
    async def boom(request):
        raise RuntimeError("boom")

    @app.get("/misuse")
    async def misuse(request):
        # Not an upgrade request: accept_upgrade must fail and the
        # error must go through regular HTTP error handling
        return request.accept_upgrade(make_echo_handler(results))

    @app.listener("after_server_start")
    async def client(app, loop):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            # Route rejects the upgrade with a regular HTTP response
            writer.write(
                b"GET /echo HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: upgrade\r\n"
                b"Upgrade: other-proto\r\n"
                b"\r\n"
            )
            await writer.drain()
            head, rest = await read_head(reader)
            results["rejected"] = head
            writer.close()
            await writer.wait_closed()

            # Exception in the route before 101: regular 500 response
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /boom HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: upgrade\r\n"
                b"Upgrade: echo\r\n"
                b"\r\n"
            )
            await writer.drain()
            head, _ = await read_head(reader)
            results["boom"] = head
            writer.close()
            await writer.wait_closed()

            # accept_upgrade on a non-upgrade request: HTTP 500
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /misuse HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            await writer.drain()
            head, _ = await read_head(reader)
            results["misuse"] = head
            writer.close()
            await writer.wait_closed()
        finally:
            app.stop()

    app.run(single_process=True, port=port)

    assert results["rejected"].startswith(b"HTTP/1.1 426")
    assert results["boom"].startswith(b"HTTP/1.1 500")
    assert results["misuse"].startswith(b"HTTP/1.1 500")
    assert "handler_done" not in results


def test_upgrade_handler_exception_releases_transport(app, port):
    """If the takeover handler fails after 101, Sanic logs the error
    and releases the transport (client observes EOF)."""
    results = {}

    async def broken(connection: UpgradeConnection):
        results["handler_started"] = True
        raise RuntimeError("takeover failed")

    @app.get("/echo")
    async def echo_route(request):
        return request.accept_upgrade(broken)

    @app.listener("after_server_start")
    async def client(app, loop):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(UPGRADE_REQUEST)
            await writer.drain()
            head, _ = await read_head(reader)
            results["head"] = head
            # Server must close the connection after the failure
            results["eof"] = await reader.read()
            writer.close()
            await writer.wait_closed()
        finally:
            app.stop()

    app.run(single_process=True, port=port)

    assert results["head"].startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert results["handler_started"] is True
    assert results["eof"] == b""


def test_upgrade_server_stop_releases_transport(app, port):
    """On server stop, Sanic closes the transport of an upgraded
    connection so the takeover handler can finish."""
    results = {}
    add_echo_route(app, results)

    @app.listener("after_server_start")
    async def client(app, loop):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(UPGRADE_REQUEST)
        await writer.drain()
        head, _ = await read_head(reader)
        results["head"] = head
        writer.write(b"ping")
        await writer.drain()
        results["echo"] = await reader.readexactly(4)
        # Stop the server while the upgraded connection is still open
        app.stop()

    app.run(single_process=True, port=port)

    assert results["head"].startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert results["echo"] == b"ping"
    assert results["handler_done"] is True


def test_upgrade_handshake_interrupted(app, port):
    """A client that disappears mid-handshake must not hang the server
    or break subsequent requests."""
    results = {}
    add_echo_route(app, results)

    @app.get("/health")
    async def health(request):
        return text("ok")

    @app.listener("after_server_start")
    async def client(app, loop):
        try:
            # Interrupt before the request head is even complete
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /echo HTTP/1.1\r\nHost: 127.0.0.1\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            # Interrupt right after sending the upgrade request,
            # without reading the 101 response
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(UPGRADE_REQUEST)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

            await asyncio.sleep(0.1)

            # The server is still alive and speaks HTTP
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /health HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            await writer.drain()
            head, _ = await read_head(reader)
            results["health"] = head
            writer.close()
            await writer.wait_closed()
        finally:
            app.stop()

    app.run(single_process=True, port=port)

    assert results["health"].startswith(b"HTTP/1.1 200")
