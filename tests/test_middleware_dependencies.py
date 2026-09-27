import pytest

from sanic import Blueprint, Sanic
from sanic.exceptions import MiddlewareFinalizationError
from sanic.middleware import Middleware
from sanic.response import text


@pytest.fixture(autouse=True)
def reset_middleware():
    yield
    Middleware.reset_count()


def make_recorder(order, ident):
    async def record(request):
        order.append(ident)

    return record


def make_response_recorder(order, ident):
    async def record(request, response):
        order.append(ident)

    return record


@pytest.mark.asyncio
async def test_request_middleware_before_after(app: Sanic):
    order = []

    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder(order, "third"), name="third")
    app.on_request(make_recorder(order, "first"), name="first", before="third")
    app.on_request(
        make_recorder(order, "second"),
        name="second",
        after="first",
        before="third",
    )

    _, response = await app.asgi_client.get("/")

    assert response.status == 200
    assert order == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_unconstrained_middleware_keep_priority_order(app: Sanic):
    order = []

    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder(order, "m1"), name="m1", priority=1)
    app.on_request(make_recorder(order, "m2"), priority=5)
    app.on_request(make_recorder(order, "m3"), priority=1)
    app.on_request(make_recorder(order, "m4"), before="m1")

    await app.asgi_client.get("/")

    # m4 is moved ahead of m1 by its constraint, while every middleware
    # without dependencies keeps its priority/registration order
    assert order == ["m2", "m3", "m4", "m1"]


@pytest.mark.asyncio
async def test_response_middleware_before_after(app: Sanic):
    order = []

    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_response(make_response_recorder(order, "r1"), name="r1")
    app.on_response(make_response_recorder(order, "r2"))
    app.on_response(make_response_recorder(order, "r3"), name="r3", after="r1")

    await app.asgi_client.get("/")

    # Response middleware run in reverse registration order by default,
    # r3 is moved after r1 by its constraint
    assert order == ["r2", "r1", "r3"]


def test_unknown_before_dependency_raises(app: Sanic):
    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder([], "lonely"), name="lonely", before="ghost")

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    message = str(excinfo.value)
    assert "'ghost'" in message
    assert "Dependency chain: lonely -> ghost" in message


def test_unknown_after_dependency_raises(app: Sanic):
    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder([], "lonely"), name="lonely", after="ghost")

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    message = str(excinfo.value)
    assert "'ghost'" in message
    assert "Dependency chain: ghost -> lonely" in message


def test_dependency_cycle_raises_with_full_chain(app: Sanic):
    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder([], "a"), name="a", before="b")
    app.on_request(make_recorder([], "b"), name="b", before="c")
    app.on_request(make_recorder([], "c"), name="c", before="a")

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    message = str(excinfo.value)
    assert "cycle" in message
    assert "Dependency chain: a -> b -> c -> a" in message


def test_self_dependency_cycle_raises(app: Sanic):
    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder([], "a"), name="a", after="a")

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    assert "Dependency chain: a -> a" in str(excinfo.value)


def test_duplicate_middleware_name_raises(app: Sanic):
    @app.get("/")
    async def handler(request):
        return text("OK")

    app.on_request(make_recorder([], "one"), name="dup")
    app.on_request(make_recorder([], "two"), name="dup", before="dup")

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    assert "'dup'" in str(excinfo.value)


@pytest.mark.asyncio
async def test_blueprint_middleware_dependencies(app: Sanic):
    order = []
    bp = Blueprint("bp_deps", url_prefix="/bp")

    @app.get("/app")
    async def app_handler(request):
        return text("app")

    @bp.get("/route")
    async def bp_handler(request):
        return text("bp")

    app.on_request(make_recorder(order, "app_mw"), name="app_mw")
    bp.middleware("request", name="bp_mw", after="app_mw")(
        make_recorder(order, "bp_mw")
    )
    bp.middleware("request")(make_recorder(order, "bp_plain"))

    app.blueprint(bp)

    _, response = await app.asgi_client.get("/bp/route")
    assert response.status == 200
    assert order == ["app_mw", "bp_mw", "bp_plain"]

    order.clear()
    _, response = await app.asgi_client.get("/app")
    assert response.status == 200
    assert order == ["app_mw"]


@pytest.mark.asyncio
async def test_app_middleware_dependencies_on_blueprint_middleware(app: Sanic):
    order = []
    bp = Blueprint("bp_app_ref", url_prefix="/bp")

    @bp.get("/route")
    async def bp_handler(request):
        return text("bp")

    bp.middleware("request", name="bp_mw")(make_recorder(order, "bp_mw"))
    app.on_request(
        make_recorder(order, "app_mw"), name="app_mw", before="bp_mw"
    )

    app.blueprint(bp)

    _, response = await app.asgi_client.get("/bp/route")
    assert response.status == 200
    assert order == ["app_mw", "bp_mw"]


def test_app_level_unknown_dependency_is_checked_on_merge(app: Sanic):
    bp = Blueprint("bp_unknown", url_prefix="/bp")

    @bp.get("/route")
    async def bp_handler(request):
        return text("bp")

    app.on_request(make_recorder([], "app_mw"), name="app_mw", before="ghost")

    app.blueprint(bp)

    with pytest.raises(MiddlewareFinalizationError) as excinfo:
        app.finalize_middleware()

    assert "Dependency chain: app_mw -> ghost" in str(excinfo.value)


@pytest.mark.asyncio
async def test_nested_blueprint_group_middleware_dependencies(app: Sanic):
    order = []
    bp1 = Blueprint("bp1", url_prefix="/bp1")
    bp2 = Blueprint("bp2", url_prefix="/bp2")
    bp3 = Blueprint("bp3", url_prefix="/bp3")
    inner_group = Blueprint.group(bp1, bp2)
    outer_group = Blueprint.group(inner_group, bp3)

    @bp1.get("/route")
    async def bp1_handler(request):
        return text("bp1")

    @bp2.get("/route")
    async def bp2_handler(request):
        return text("bp2")

    @bp3.get("/route")
    async def bp3_handler(request):
        return text("bp3")

    outer_group.middleware("request", name="group_mw")(
        make_recorder(order, "group_mw")
    )
    bp1.middleware("request", name="bp1_mw", after="group_mw")(
        make_recorder(order, "bp1_mw")
    )
    bp2.middleware("request", name="bp2_mw", before="group_mw")(
        make_recorder(order, "bp2_mw")
    )

    app.blueprint(outer_group)

    _, response = await app.asgi_client.get("/bp1/route")
    assert response.status == 200
    assert order == ["group_mw", "bp1_mw"]

    order.clear()
    _, response = await app.asgi_client.get("/bp2/route")
    assert response.status == 200
    assert order == ["bp2_mw", "group_mw"]

    order.clear()
    _, response = await app.asgi_client.get("/bp3/route")
    assert response.status == 200
    assert order == ["group_mw"]


@pytest.mark.asyncio
async def test_same_function_registered_in_multiple_locations(app: Sanic):
    order = []
    bp = Blueprint("bp_shared", url_prefix="/bp")

    async def shared(request):
        order.append("shared")

    async def shared_response(request, response):
        order.append("shared_response")

    @bp.get("/route")
    async def bp_handler(request):
        return text("bp")

    app.on_request(shared, name="app_shared")
    bp.middleware("request", name="bp_shared", after="app_shared")(shared)
    app.on_response(shared_response, name="app_shared_response")
    bp.middleware("response", name="bp_shared_response")(shared_response)

    app.blueprint(bp)

    _, response = await app.asgi_client.get("/bp/route")
    assert response.status == 200
    assert order == [
        "shared",
        "shared",
        "shared_response",
        "shared_response",
    ]


@pytest.mark.asyncio
async def test_short_circuit_response_middleware_constraints(app: Sanic):
    order = []

    @app.get("/")
    async def handler(request):
        order.append("handler")
        return text("handler")

    @app.on_request(name="gate", priority=99)
    async def gate(request):
        order.append("gate")
        return text("stopped")

    app.on_response(make_response_recorder(order, "r1"), name="r1")
    app.on_response(make_response_recorder(order, "r2"))
    app.on_response(make_response_recorder(order, "r3"), name="r3", after="r1")

    _, response = await app.asgi_client.get("/")

    assert response.status == 200
    assert response.text == "stopped"
    # The handler never ran, but the response middleware that did run
    # still honor their declared constraints
    assert order == ["gate", "r2", "r1", "r3"]


@pytest.mark.asyncio
async def test_refinalize_does_not_drift(app: Sanic):
    order = []
    bp = Blueprint("bp_drift", url_prefix="/bp")

    @bp.get("/route")
    async def bp_handler(request):
        return text("bp")

    app.on_request(make_recorder(order, "m1"), name="m1", priority=2)
    app.on_request(make_recorder(order, "m2"), name="m2", before="m1")
    app.on_request(make_recorder(order, "m3"))
    bp.middleware("request", name="bp_mw", after="m2")(
        make_recorder(order, "bp_mw")
    )
    app.on_response(make_response_recorder(order, "r1"), name="r1")
    app.on_response(make_response_recorder(order, "r2"), name="r2", after="r1")

    app.blueprint(bp)

    def snapshot():
        route = next(
            route
            for route in app.router.routes
            if route.name.endswith("bp_handler")
        )
        return (
            [middleware.name for middleware in app.request_middleware],
            [middleware.name for middleware in app.response_middleware],
            [middleware.name for middleware in route.extra.request_middleware],
            [
                middleware.name
                for middleware in route.extra.response_middleware
            ],
        )

    app.finalize_middleware()
    first_snapshot = snapshot()

    # Re-preparing the application (as happens in dev mode reloads)
    # must not change the resolved order
    app.finalize_middleware()
    assert snapshot() == first_snapshot

    _, response = await app.asgi_client.get("/bp/route")
    assert response.status == 200
    assert order == ["m2", "m1", "m3", "bp_mw", "r1", "r2"]

    # The resolved order stays stable across further re-preparations,
    # even after the application has served requests
    app.finalize_middleware()
    second_snapshot = snapshot()
    app.finalize_middleware()
    assert snapshot() == second_snapshot

    order.clear()
    _, response = await app.asgi_client.get("/bp/route")
    assert response.status == 200
    assert order == ["m2", "m1", "m3", "bp_mw", "r1", "r2"]
