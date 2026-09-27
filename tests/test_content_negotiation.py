import pytest

from sanic_routing.exceptions import RouteExists

from sanic import Blueprint, Sanic, json, text
from sanic.exceptions import NotAcceptable, UnsupportedMediaType


@pytest.fixture
def import_app():
    app = Sanic("import_app")

    @app.post(
        "/import",
        consumes="application/json",
        produces="application/json",
    )
    async def import_json(request):
        return json({"handler": "json"})

    @app.post("/import", consumes="text/csv", produces="text/csv")
    async def import_csv(request):
        return text("handler: csv")

    return app


@pytest.mark.asyncio
async def test_content_type_selects_handler(import_app):
    _, response = await import_app.asgi_client.post(
        "/import",
        json={"a": 1},
        headers={"Accept": "application/json"},
    )
    assert response.status == 200
    assert response.json == {"handler": "json"}

    _, response = await import_app.asgi_client.post(
        "/import",
        data="a,b",
        headers={"Content-Type": "text/csv", "Accept": "text/csv"},
    )
    assert response.status == 200
    assert response.text == "handler: csv"


@pytest.mark.asyncio
async def test_interleaved_requests_do_not_stick_to_first_negotiation(
    import_app,
):
    """The first negotiated result must not decide later requests."""
    expected = []
    for _ in range(3):
        expected.append(("json", {"handler": "json"}))
        expected.append(("csv", "handler: csv"))

    results = []
    for kind, _ in expected:
        if kind == "json":
            _, response = await import_app.asgi_client.post(
                "/import",
                json={"a": 1},
                headers={"Accept": "application/json"},
            )
            results.append((kind, response.json))
        else:
            _, response = await import_app.asgi_client.post(
                "/import",
                data="a,b",
                headers={"Content-Type": "text/csv", "Accept": "text/csv"},
            )
            results.append((kind, response.text))

    assert results == expected
    # Repeated identical requests are served from the router cache
    assert import_app.router.get.cache_info().hits > 0


@pytest.mark.asyncio
async def test_accept_q_values_and_wildcards():
    app = Sanic("accept_app")

    @app.get("/report", produces="application/json")
    async def report_json(request):
        return json({"handler": "json"}, headers={"X-Handler": "json"})

    @app.get("/report", produces="text/csv")
    async def report_csv(request):
        return text("a,b", headers={"X-Handler": "csv"})

    _, response = await app.asgi_client.get(
        "/report",
        headers={"Accept": "text/csv;q=0.9, application/json;q=0.1"},
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "csv"

    _, response = await app.asgi_client.get(
        "/report",
        headers={"Accept": "text/csv;q=0.1, application/json;q=0.9"},
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "json"

    _, response = await app.asgi_client.get(
        "/report", headers={"Accept": "text/*"}
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "csv"

    # No Accept header: first registered variant wins
    _, response = await app.asgi_client.get("/report")
    assert response.status == 200
    assert response.headers["X-Handler"] == "json"


@pytest.mark.asyncio
async def test_accept_parameters():
    app = Sanic("accept_params_app")

    @app.get("/report", produces="text/csv;charset=utf-8")
    async def report_utf8(request):
        return text("utf8", headers={"X-Handler": "utf8"})

    @app.get("/report", produces="text/csv")
    async def report_plain(request):
        return text("plain", headers={"X-Handler": "plain"})

    _, response = await app.asgi_client.get(
        "/report", headers={"Accept": "text/csv;charset=utf-8"}
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "utf8"

    _, response = await app.asgi_client.get(
        "/report", headers={"Accept": "text/csv"}
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "plain"


@pytest.mark.asyncio
async def test_consumes_wildcard():
    app = Sanic("consumes_wildcard_app")

    @app.post("/import", consumes="application/*")
    async def import_any(request):
        return text("ok")

    _, response = await app.asgi_client.post(
        "/import", json={"a": 1}, headers={"Accept": "*/*"}
    )
    assert response.status == 200

    _, response = await app.asgi_client.post(
        "/import", data="a,b", headers={"Content-Type": "text/csv"}
    )
    assert response.status == 415


@pytest.mark.asyncio
async def test_unsupported_media_type(import_app):
    _, response = await import_app.asgi_client.post(
        "/import", data="x", headers={"Content-Type": "text/plain"}
    )
    assert response.status == 415


@pytest.mark.asyncio
async def test_not_acceptable(import_app):
    _, response = await import_app.asgi_client.post(
        "/import",
        json={"a": 1},
        headers={"Accept": "image/png"},
    )
    assert response.status == 406


@pytest.mark.asyncio
async def test_not_found_and_method_not_allowed_unchanged(import_app):
    _, response = await import_app.asgi_client.get("/does-not-exist")
    assert response.status == 404

    # Method mismatch still takes the 405 path, not content negotiation
    _, response = await import_app.asgi_client.put(
        "/import",
        data="a,b",
        headers={"Content-Type": "text/csv", "Accept": "text/csv"},
    )
    assert response.status == 405


def test_conflicting_variants_rejected_at_registration():
    app = Sanic("conflict_app")

    @app.post("/import", consumes="application/json")
    async def import_json(request):
        return json({})

    message = (
        "Route already registered: POST /import with "
        r"consumes=\('application/json',\)"
    )
    with pytest.raises(RouteExists, match=message):

        @app.post("/import", consumes="application/json")
        async def import_json_again(request):
            return json({})


def test_duplicate_route_without_media_types_still_rejected():
    app = Sanic("duplicate_app")

    @app.get("/thing")
    async def thing(request):
        return text("one")

    with pytest.raises(RouteExists):

        @app.get("/thing")
        async def thing_again(request):
            return text("two")


@pytest.mark.asyncio
async def test_blueprint_content_negotiation():
    bp = Blueprint("import_bp", url_prefix="/bp")

    @bp.post(
        "/import", consumes="application/json", produces="application/json"
    )
    async def import_json(request):
        return json({"handler": "json"})

    @bp.post("/import", consumes="text/csv", produces="text/csv")
    async def import_csv(request):
        return text("handler: csv")

    app = Sanic("bp_app")
    app.blueprint(bp)

    _, response = await app.asgi_client.post(
        "/bp/import", json={"a": 1}, headers={"Accept": "application/json"}
    )
    assert response.status == 200
    assert response.json == {"handler": "json"}

    _, response = await app.asgi_client.post(
        "/bp/import",
        data="a,b",
        headers={"Content-Type": "text/csv", "Accept": "text/csv"},
    )
    assert response.status == 200
    assert response.text == "handler: csv"


@pytest.mark.asyncio
async def test_head_uses_get_negotiation():
    app = Sanic("head_app")

    @app.get("/report", produces="application/json")
    async def report_json(request):
        return json({"handler": "json"}, headers={"X-Handler": "json"})

    @app.get("/report", produces="text/csv")
    async def report_csv(request):
        return text("a,b", headers={"X-Handler": "csv"})

    _, response = await app.asgi_client.head(
        "/report", headers={"Accept": "text/csv"}
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "csv"

    _, response = await app.asgi_client.head(
        "/report", headers={"Accept": "application/json"}
    )
    assert response.status == 200
    assert response.headers["X-Handler"] == "json"


@pytest.mark.asyncio
async def test_head_on_regular_route_still_405():
    app = Sanic("head_plain_app")

    @app.get("/plain")
    async def plain(request):
        return text("plain")

    _, response = await app.asgi_client.head("/plain")
    assert response.status == 405


def test_negotiation_exceptions_are_http_exceptions():
    assert NotAcceptable.status_code == 406
    assert UnsupportedMediaType.status_code == 415
