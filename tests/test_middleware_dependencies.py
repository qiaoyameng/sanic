import pytest

from sanic import Blueprint, Sanic
from sanic.exceptions import SanicException
from sanic.middleware import Middleware
from sanic.response import text


@pytest.fixture(autouse=True)
def reset_middleware():
    yield
    Middleware.reset_count()


@pytest.mark.asyncio
async def test_request_middleware_before_after(app: Sanic):
    order = []

    @app.on_request(name="auth")
    async def auth(request):
        order.append("auth")

    @app.on_request(after="auth")
    async def after_auth(request):
        order.append("after_auth")

    @app.on_request(before="auth")
    async def before_auth(request):
        order.append("before_auth")

    @app.get("/")
    def handler(request):
        return text("ok")

    _, response = await app.asgi_client.get("/")

    assert response.status == 200
    assert order == ["before_auth", "auth", "after_auth"]


@pytest.mark.asyncio
async def test_response_middleware_before_after(app: Sanic):
    order = []

    @app.on_response(name="headers")
    async def headers(request, response):
        order.append("headers")

    @app.on_response(after="headers")
    async def after_headers(request, response):
        order.append("after_headers")

    @app.on_response(before="headers")
    async def before_headers(request, response):
        order.append("before_headers")

    @app.get("/")
    def handler(request):
        return text("ok")

    _, response = await app.asgi_client.get("/")

    assert response.status == 200
    assert order == ["before_headers", "headers", "after_headers"]


@pytest.mark.asyncio
async def test_middleware_multiple_dependencies(app: Sanic):
    order = []

    @app.on_request(name="one")
    async def one(request):
        order.append("one")

    @app.on_request(name="two")
    async def two(request):
        order.append("two")

    @app.on_request(after=["one", "two"])
    async def three(request):
        order.append("three")

    @app.on_request(before=["one", "two"])
    async def zero(request):
        order.append("zero")

    @app.get("/")
    def handler(request):
        return text("ok")

    await app.asgi_client.get("/")

    assert order == ["zero", "one", "two", "three"]


@pytest.mark.asyncio
async def test_unconstrained_middleware_keeps_priority_order(app: Sanic):
    order = []

    @app.on_request(priority=10)
    async def high(request):
        order.append("high")

    @app.on_request(name="second", after="first")
    async def second(request):
        order.append("second")

    @app.on_request(name="first")
    async def first(request):
        order.append("first")

    @app.on_request(priority=5)
    async def low(request):
        order.append("low")

    @app.get("/")
    def handler(request):
        return text("ok")

    await app.asgi_client.get("/")

    assert order == ["high", "low", "first", "second"]


def test_unknown_dependency_raises(app: Sanic):
    @app.on_request(name="known", after="missing")
    async def known(request): ...

    @app.get("/")
    def handler(request):
        return text("ok")

    with pytest.raises(SanicException) as excinfo:
        app.finalize_middleware()

    message = excinfo.value.args[0]
    assert "unknown name" in message
    assert "known -> missing" in message
    assert "after" in message


def test_dependency_cycle_raises(app: Sanic):
    @app.on_request(name="a", after="c")
    async def a(request): ...

    @app.on_request(name="b", after="a")
    async def b(request): ...

    @app.on_request(name="c", after="b")
    async def c(request): ...

    @app.get("/")
    def handler(request):
        return text("ok")

    with pytest.raises(SanicException) as excinfo:
        app.finalize_middleware()

    message = excinfo.value.args[0]
    assert "cycle" in message
    # The full dependency chain must be reported, beginning and ending
    # with the same middleware
    chain = message.split(":", 1)[1].strip().split(" -> ")
    assert len(chain) == 4
    assert chain[0] == chain[-1]
    assert set(chain) == {"a", "b", "c"}


def test_duplicate_name_different_functions_raises(app: Sanic):
    @app.on_request(name="dup")
    async def one(request): ...

    @app.on_request(name="dup")
    async def two(request): ...

    @app.get("/")
    def handler(request):
        return text("ok")

    with pytest.raises(SanicException, match="dup"):
        app.finalize_middleware()


@pytest.mark.asyncio
async def test_app_and_blueprint_dependencies_merge(app: Sanic):
    order = []
    bp = Blueprint("bp", url_prefix="/bp")

    @app.on_request(name="app_mw")
    async def app_mw(request):
        order.append("app_mw")

    @bp.on_request(after="app_mw")
    async def bp_mw(request):
        order.append("bp_mw")

    @bp.on_request(before="app_mw")
    async def bp_first(request):
        order.append("bp_first")

    @bp.get("/")
    def handler(request):
        return text("ok")

    app.blueprint(bp)

    _, response = await app.asgi_client.get("/bp")

    assert response.status == 200
    assert order == ["bp_first", "app_mw", "bp_mw"]


@pytest.mark.asyncio
async def test_nested_blueprint_dependencies(app: Sanic):
    order = []
    bp_one = Blueprint("one", url_prefix="/one")
    bp_two = Blueprint("two", url_prefix="/two")

    @app.on_request(name="app_mw")
    async def app_mw(request):
        order.append("app_mw")

    @bp_one.on_request(after="app_mw")
    async def bp_one_mw(request):
        order.append("bp_one_mw")

    @bp_two.on_request(before="app_mw")
    async def bp_two_mw(request):
        order.append("bp_two_mw")

    @bp_one.get("/")
    def one_handler(request):
        return text("one")

    @bp_two.get("/")
    def two_handler(request):
        return text("two")

    app.blueprint(Blueprint.group(bp_one, bp_two, url_prefix="/api"))

    _, response = await app.asgi_client.get("/api/one")
    assert response.status == 200
    assert order == ["app_mw", "bp_one_mw"]

    order.clear()
    _, response = await app.asgi_client.get("/api/two")
    assert response.status == 200
    assert order == ["bp_two_mw", "app_mw"]


def test_blueprint_unknown_dependency_raises_after_merge(app: Sanic):
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.on_request(after="missing")
    async def bp_mw(request): ...

    @bp.get("/")
    def handler(request):
        return text("ok")

    app.blueprint(bp)

    with pytest.raises(SanicException) as excinfo:
        app.finalize_middleware()

    assert "bp_mw -> missing" in excinfo.value.args[0]


def test_blueprint_dependency_cycle_raises_after_merge(app: Sanic):
    bp = Blueprint("bp", url_prefix="/bp")

    @app.on_request(name="app_mw", after="bp_mw")
    async def app_mw(request): ...

    @bp.on_request(name="bp_mw", after="app_mw")
    async def bp_mw(request): ...

    @bp.get("/")
    def handler(request):
        return text("ok")

    app.blueprint(bp)

    with pytest.raises(SanicException) as excinfo:
        app.finalize_middleware()

    message = excinfo.value.args[0]
    assert "cycle" in message
    assert "app_mw" in message
    assert "bp_mw" in message


@pytest.mark.asyncio
async def test_same_function_registered_in_multiple_locations(app: Sanic):
    calls = []
    bp = Blueprint("bp", url_prefix="/bp")

    def shared(request, response=None):
        calls.append("shared")

    app.on_request(shared, name="shared")
    app.on_response(shared, name="shared")
    bp.on_request(shared, name="shared")

    @app.on_request(after="shared")
    async def after_shared(request):
        calls.append("after_shared")

    @bp.get("/")
    def handler(request):
        return text("ok")

    app.blueprint(bp)

    _, response = await app.asgi_client.get("/bp")

    assert response.status == 200
    # The same function may be registered on the app and on a blueprint
    # under the same name; dependents wait for every registration of it
    assert calls == ["shared", "shared", "after_shared", "shared"]


@pytest.mark.asyncio
async def test_short_circuit_response_middleware_constraints(app: Sanic):
    order = []

    @app.on_request
    async def stopper(request):
        return text("stopped")

    @app.on_response(name="second")
    async def second(request, response):
        order.append("second")

    @app.on_response(before="second")
    async def first(request, response):
        order.append("first")

    @app.get("/")
    def handler(request):
        return text("never reached")

    _, response = await app.asgi_client.get("/")

    assert response.status == 200
    assert response.text == "stopped"
    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_finalize_middleware_is_stable_across_reprepare(app: Sanic):
    order = []

    @app.on_request(name="z", after="a")
    async def z(request):
        order.append("z")

    @app.on_request(name="a")
    async def a(request):
        order.append("a")

    @app.on_request
    async def plain(request):
        order.append("plain")

    @app.get("/")
    def handler(request):
        return text("ok")

    await app.asgi_client.get("/")
    assert order == ["a", "z", "plain"]

    # Re-preparing the application (as happens in debug/reload mode)
    # must not drift the resolved order
    app.finalize_middleware()
    app.finalize_middleware()

    order.clear()
    await app.asgi_client.get("/")
    assert order == ["a", "z", "plain"]


@pytest.mark.asyncio
async def test_register_middleware_with_dependencies(app: Sanic):
    order = []

    def first(request):
        order.append("first")

    def second(request):
        order.append("second")

    app.register_middleware(second, name="second")
    app.register_middleware(first, before="second")

    @app.get("/")
    def handler(request):
        return text("ok")

    await app.asgi_client.get("/")

    assert order == ["first", "second"]


@pytest.mark.asyncio
async def test_register_named_middleware_with_dependencies(app: Sanic):
    order = []

    def first(request):
        order.append("first")

    def second(request):
        order.append("second")

    @app.get("/")
    def handler(request):
        return text("ok")

    route_name = app.router.routes[0].name
    app.register_named_middleware(second, [route_name], name="second")
    app.register_named_middleware(first, [route_name], before="second")

    await app.asgi_client.get("/")

    assert order == ["first", "second"]
