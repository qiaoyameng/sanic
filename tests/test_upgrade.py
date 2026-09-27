import asyncio

from collections import namedtuple

import pytest

from sanic_testing.reusable import ReusableClient

from sanic import Upgrade, text
from sanic.app import Sanic
from tests.client import RawClient


@pytest.fixture
def test_app(app: Sanic):
    async def echo_protocol(request, connection):
        async for data in connection:
            await connection.send(data)

    async def failing_protocol(request, connection):
        raise RuntimeError("protocol exploded")

    async def closing_protocol(request, connection):
        await connection.close()

    @app.get("/echo")
    async def echo(request):
        if request.headers.get("upgrade") != "echo":
            return text("expected upgrade", status=400)
        return Upgrade(echo_protocol)

    @app.get("/reject")
    async def reject(request):
        return text("no upgrade for you", status=426)

    @app.get("/boom")
    async def boom(request):
        raise RuntimeError("handler exploded")

    @app.get("/naive")
    async def naive(request):
        # Returns an Upgrade even though the client may not have
        # asked for one
        return Upgrade(echo_protocol)

    @app.get("/fail")
    async def fail(request):
        return Upgrade(failing_protocol)

    @app.get("/close")
    async def close(request):
        return Upgrade(closing_protocol)

    return app


@pytest.fixture
def runner(test_app: Sanic, port):
    client = ReusableClient(test_app, port=port)
    client.run()
    yield client
    client.stop()


@pytest.fixture
def client(runner: ReusableClient):
    client = namedtuple(
        "Client",
        ("raw", "send", "recv_until", "recv_exactly", "recv_all", "pump"),
    )

    raw = RawClient(runner.host, runner.port)
    runner._run(raw.connect())

    def send(msg):
        runner._run(raw.send(msg))

    def recv_until(separator: bytes):
        return runner._run(raw.reader.readuntil(separator))

    def recv_exactly(nbytes: int):
        return runner._run(raw.reader.readexactly(nbytes))

    def recv_all():
        return runner._run(raw.reader.read(-1))

    def pump(duration: float = 0.1):
        runner._run(asyncio.sleep(duration))

    yield client(raw, send, recv_until, recv_exactly, recv_all, pump)

    try:
        if not raw.writer.is_closing():
            runner._run(raw.close())
    except Exception:
        pass


def upgrade_request(path: str, protocol: str = "echo") -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        "host: localhost\r\n"
        "connection: upgrade\r\n"
        f"upgrade: {protocol}\r\n"
        "\r\n"
    ).encode()


def test_upgrade_echo(client):
    client.send(upgrade_request("/echo"))
    response = client.recv_until(b"\r\n\r\n")

    assert b"101 Switching Protocols" in response
    assert b"connection: upgrade" in response
    assert b"upgrade: echo" in response

    payload = b"\x00\x01\x02binary\xff"
    client.send(payload)
    assert client.recv_exactly(len(payload)) == payload

    client.send(b"more data")
    assert client.recv_exactly(9) == b"more data"


def test_upgrade_early_data_same_packet(client):
    # The client sends protocol bytes in the same write as the request
    # headers, before the 101 response was sent. They must not be lost.
    early = b"\xde\xad\xbe\xef early bytes"
    client.send(upgrade_request("/echo") + early)

    response = client.recv_until(b"\r\n\r\n")
    assert b"101 Switching Protocols" in response

    assert client.recv_exactly(len(early)) == early

    # The connection still works both ways afterwards
    client.send(b"ping")
    assert client.recv_exactly(4) == b"ping"


def test_upgrade_rejected_with_http_response(client):
    client.send(upgrade_request("/reject"))
    response = client.recv_until(b"\r\n\r\n")

    assert b"426" in response
    assert b"101" not in response


def test_upgrade_rejected_by_route_logic(client):
    # The route declines the upgrade because the requested protocol
    # is not supported
    client.send(upgrade_request("/echo", protocol="gopher"))
    response = client.recv_until(b"\r\n\r\n")

    assert b"400" in response
    assert b"101" not in response


def test_upgrade_handler_exception_before_101(client):
    # Errors raised by the route before the 101 is sent go through
    # the regular HTTP error handling
    client.send(upgrade_request("/boom"))
    response = client.recv_until(b"\r\n\r\n")

    assert b"500" in response
    assert b"101" not in response


def test_upgrade_without_upgrade_header(client):
    # The handler returned an Upgrade, but the client did not ask for
    # one: this is a server error, handled as a regular HTTP error
    client.send(b"GET /naive HTTP/1.1\r\nhost: localhost\r\n\r\n")
    response = client.recv_until(b"\r\n\r\n")

    assert b"500" in response
    assert b"101" not in response


def test_upgrade_takeover_failure(client, test_app):
    # The upgrade handler fails after the 101 was sent: no HTTP error
    # response may follow, the server closes the connection instead
    client.send(upgrade_request("/fail"))
    response = client.recv_until(b"\r\n\r\n")
    assert b"101 Switching Protocols" in response

    assert client.recv_all() == b""
    client.pump()
    assert test_app.upgrade_tasks == set()


def test_upgrade_connection_closed_by_handler(client):
    # The upgrade handler closes the connection itself
    client.send(upgrade_request("/close"))
    response = client.recv_until(b"\r\n\r\n")
    assert b"101 Switching Protocols" in response

    assert client.recv_all() == b""


def test_upgrade_handshake_aborted_partial_request(client, runner):
    # The client disconnects in the middle of the request headers.
    # The server must survive and keep serving new connections.
    client.send(b"GET /echo HTTP/1.1\r\nhost: localh")
    runner._run(client.raw.close())
    client.pump()

    raw = RawClient(runner.host, runner.port)
    runner._run(raw.connect())
    try:
        runner._run(raw.send(upgrade_request("/echo")))
        response = runner._run(raw.reader.readuntil(b"\r\n\r\n"))
        assert b"101 Switching Protocols" in response
    finally:
        runner._run(raw.close())


def test_upgrade_handshake_aborted_before_101_read(client, runner, test_app):
    # The client sends a complete upgrade request but disconnects
    # before reading the 101 response. The server must release the
    # connection and the handler task.
    client.send(upgrade_request("/echo"))
    runner._run(client.raw.close())

    for _ in range(10):
        client.pump()
        if not test_app.upgrade_tasks:
            break
    assert test_app.upgrade_tasks == set()


def test_upgrade_server_stop(client, runner, test_app):
    # Stopping the server cancels the upgrade handler and releases
    # the transport
    client.send(upgrade_request("/echo"))
    response = client.recv_until(b"\r\n\r\n")
    assert b"101 Switching Protocols" in response

    client.send(b"ping")
    assert client.recv_exactly(4) == b"ping"

    runner.stop()

    assert client.recv_all() == b""
    assert test_app.upgrade_tasks == set()
