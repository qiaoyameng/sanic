import pytest

from sanic_routing.exceptions import RouteExists

from sanic import Blueprint, Sanic
from sanic.response import json, text


JSON = "application/json"
CSV = "text/csv"


@pytest.fixture
def app():
    return Sanic("content-negotiation")


def make_import_app(name="import-app"):
    app = Sanic(name)

    @app.post("/import", consumes=JSON, produces=JSON)
    async def import_json(request):
        return json({"handler": "json"})

    @app.post("/import", consumes=CSV, produces=CSV)
    async def import_csv(request):
        return text("handler=csv", content_type=CSV)

    return app


@pytest.mark.asyncio
async def test_same_path_and_method_multiple_handlers():
    app = make_import_app()
    async with app.asgi_client as client:
        _, response = await client.post(
            "/import", json={"a": 1}, headers={"Accept": JSON}
        )
        assert response.status_code == 200
        assert response.json == {"handler": "json"}

        _, response = await client.post(
            "/import",
            content="a,b",
            headers={"Content-Type": CSV, "Accept": CSV},
        )
        assert response.status_code == 200
        assert response.text == "handler=csv"


@pytest.mark.asyncio
async def test_interleaved_requests_do_not_stick_to_first_negotiation():
    app = make_import_app()
    async with app.asgi_client as client:
        for _ in range(3):
            _, response = await client.post(
                "/import", json={"a": 1}, headers={"Accept": JSON}
            )
            assert response.status_code == 200
            assert response.json == {"handler": "json"}

            _, response = await client.post(
                "/import",
                content="a,b",
                headers={"Content-Type": CSV, "Accept": CSV},
            )
            assert response.status_code == 200
            assert response.text == "handler=csv"


def test_router_cache_negotiates_per_request_headers():
    app = make_import_app()
    app.finalize()

    json_route, json_handler, _ = app.router.get(
        "/import", "POST", None, JSON, JSON
    )
    csv_route, csv_handler, _ = app.router.get(
        "/import", "POST", None, CSV, CSV
    )
    again_route, again_handler, _ = app.router.get(
        "/import", "POST", None, JSON, JSON
    )

    assert json_handler is not csv_handler
    assert json_route is again_route
    assert json_handler is again_handler
    assert json_route.extra.consumes is not None
    assert str(json_route.extra.consumes[0]) == JSON
    assert str(csv_route.extra.consumes[0]) == CSV


@pytest.mark.asyncio
async def test_unsupported_media_type_is_415():
    app = make_import_app()
    async with app.asgi_client as client:
        _, response = await client.post(
            "/import",
            content="<a/>",
            headers={"Content-Type": "application/xml", "Accept": JSON},
        )
        assert response.status_code == 415


@pytest.mark.asyncio
async def test_not_acceptable_is_406():
    app = make_import_app()
    async with app.asgi_client as client:
        _, response = await client.post(
            "/import", json={"a": 1}, headers={"Accept": "application/xml"}
        )
        assert response.status_code == 406


@pytest.mark.asyncio
async def test_path_and_method_mismatch_still_404_and_405():
    app = make_import_app()
    async with app.asgi_client as client:
        _, response = await client.post("/does-not-exist")
        assert response.status_code == 404

        _, response = await client.get("/import", headers={"Accept": JSON})
        assert response.status_code == 405


@pytest.mark.asyncio
async def test_accept_q_values_select_handler():
    app = Sanic("q-values")

    @app.get("/doc", produces=JSON)
    async def doc_json(request):
        return json({"fmt": "json"})

    @app.get("/doc", produces="text/html")
    async def doc_html(request):
        return text("<b>doc</b>", content_type="text/html")

    async with app.asgi_client as client:
        _, response = await client.get(
            "/doc", headers={"Accept": f"text/html;q=0.9, {JSON};q=0.1"}
        )
        assert response.text == "<b>doc</b>"

        _, response = await client.get(
            "/doc", headers={"Accept": f"text/html;q=0.1, {JSON};q=0.9"}
        )
        assert response.json == {"fmt": "json"}

        # Interleave to prove q-value results are cached independently
        _, response = await client.get(
            "/doc", headers={"Accept": f"text/html;q=0.9, {JSON};q=0.1"}
        )
        assert response.text == "<b>doc</b>"


@pytest.mark.asyncio
async def test_accept_wildcards():
    app = Sanic("wildcards")

    @app.get("/doc", produces=JSON)
    async def doc_json(request):
        return json({"fmt": "json"})

    @app.get("/doc", produces="text/html")
    async def doc_html(request):
        return text("<b>doc</b>", content_type="text/html")

    async with app.asgi_client as client:
        _, response = await client.get("/doc", headers={"Accept": "text/*"})
        assert response.text == "<b>doc</b>"

        _, response = await client.get("/doc", headers={"Accept": "*/*"})
        assert response.status_code == 200

        # An explicitly empty Accept header accepts nothing
        _, response = await client.get("/doc", headers={"Accept": ""})
        assert response.status_code == 406


@pytest.mark.asyncio
async def test_accept_media_type_parameters():
    app = Sanic("accept-params")

    @app.get("/feed", produces="application/json;version=1")
    async def feed_v1(request):
        return json({"version": 1})

    @app.get("/feed", produces="application/json;version=2")
    async def feed_v2(request):
        return json({"version": 2})

    async with app.asgi_client as client:
        _, response = await client.get(
            "/feed", headers={"Accept": "application/json;version=2"}
        )
        assert response.json == {"version": 2}

        _, response = await client.get(
            "/feed", headers={"Accept": "application/json;version=1"}
        )
        assert response.json == {"version": 1}


@pytest.mark.asyncio
async def test_content_type_with_parameters_matches_consumes():
    app = Sanic("consumes-params")

    @app.post("/import", consumes=JSON, produces=JSON)
    async def import_json(request):
        return json({"ok": True})

    async with app.asgi_client as client:
        _, response = await client.post(
            "/import",
            content='{"a": 1}',
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Accept": JSON,
            },
        )
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_missing_content_type_with_consumes_is_415():
    app = Sanic("missing-content-type")

    @app.post("/import", consumes=JSON, produces=JSON)
    async def import_json(request):
        return json({"ok": True})

    async with app.asgi_client as client:
        _, response = await client.post("/import", headers={"Accept": JSON})
        assert response.status_code == 415


@pytest.mark.asyncio
async def test_handler_without_consumes_is_fallback():
    app = Sanic("fallback")

    @app.post("/import", consumes=JSON, produces=JSON)
    async def import_json(request):
        return json({"handler": "json"})

    @app.post("/import", produces="text/plain")
    async def import_any(request):
        return text("handler=fallback")

    async with app.asgi_client as client:
        _, response = await client.post(
            "/import",
            content="a,b",
            headers={"Content-Type": CSV, "Accept": "text/plain"},
        )
        assert response.status_code == 200
        assert response.text == "handler=fallback"

        _, response = await client.post(
            "/import", json={"a": 1}, headers={"Accept": JSON}
        )
        assert response.status_code == 200
        assert response.json == {"handler": "json"}


@pytest.mark.asyncio
async def test_blueprint_routes_negotiate():
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.post("/import", consumes=JSON, produces=JSON)
    async def bp_import_json(request):
        return json({"handler": "json"})

    @bp.post("/import", consumes=CSV, produces=CSV)
    async def bp_import_csv(request):
        return text("handler=csv", content_type=CSV)

    app = Sanic("bp-negotiation")
    app.blueprint(bp)

    async with app.asgi_client as client:
        _, response = await client.post(
            "/bp/import", json={"a": 1}, headers={"Accept": JSON}
        )
        assert response.status_code == 200
        assert response.json == {"handler": "json"}

        _, response = await client.post(
            "/bp/import",
            content="a,b",
            headers={"Content-Type": CSV, "Accept": CSV},
        )
        assert response.status_code == 200
        assert response.text == "handler=csv"

        _, response = await client.post(
            "/bp/import",
            content="x",
            headers={"Content-Type": "application/xml", "Accept": JSON},
        )
        assert response.status_code == 415


@pytest.mark.asyncio
async def test_head_requests_negotiate():
    app = Sanic("head-negotiation")

    @app.route("/multi", methods=["GET", "HEAD"], produces=JSON)
    async def multi_json(request):
        return json({"handler": "json"})

    @app.route("/multi", methods=["GET", "HEAD"], produces="text/html")
    async def multi_html(request):
        return text("<b>hi</b>", content_type="text/html")

    async with app.asgi_client as client:
        _, response = await client.head("/multi", headers={"Accept": JSON})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(JSON)

        _, response = await client.head(
            "/multi", headers={"Accept": "text/html"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

        _, response = await client.head(
            "/multi", headers={"Accept": "application/xml"}
        )
        assert response.status_code == 406


@pytest.mark.asyncio
async def test_middleware_still_applies_to_negotiated_routes():
    app = Sanic("negotiated-middleware")
    calls = []

    @app.middleware("request")
    async def track(request):
        calls.append(request.path)

    @app.post("/import", consumes=JSON, produces=JSON)
    async def import_json(request):
        return json({"handler": "json"})

    @app.post("/import", consumes=CSV, produces=CSV)
    async def import_csv(request):
        return text("handler=csv", content_type=CSV)

    async with app.asgi_client as client:
        await client.post("/import", json={"a": 1}, headers={"Accept": JSON})
        await client.post(
            "/import",
            content="a,b",
            headers={"Content-Type": CSV, "Accept": CSV},
        )

    assert calls == ["/import", "/import"]


def test_overlapping_consumes_conflict_at_startup():
    app = Sanic("conflict")

    @app.post("/import", consumes=JSON)
    async def import_json(request):
        return json({})

    with pytest.raises(RouteExists, match="Ambiguous route registration"):

        @app.post("/import", consumes="application/*")
        async def import_any(request):
            return text("")


def test_overlapping_produces_conflict_at_startup():
    app = Sanic("conflict-produces")

    @app.get("/doc", produces=JSON)
    async def doc_json(request):
        return json({})

    with pytest.raises(RouteExists, match="Ambiguous route registration"):

        @app.get("/doc", produces="*/*")
        async def doc_any(request):
            return text("")


def test_distinct_media_type_params_do_not_conflict():
    app = Sanic("no-conflict")

    @app.get("/feed", produces="application/json;version=1")
    async def feed_v1(request):
        return json({"version": 1})

    @app.get("/feed", produces="application/json;version=2")
    async def feed_v2(request):
        return json({"version": 2})


def test_plain_duplicate_still_raises():
    app = Sanic("plain-duplicate")

    @app.post("/import")
    async def import_one(request):
        return text("")

    with pytest.raises(RouteExists, match="Route already registered"):

        @app.post("/import")
        async def import_two(request):
            return text("")


def test_plain_route_cannot_overload_negotiated():
    app = Sanic("mixed")

    @app.post("/import", consumes=JSON)
    async def import_json(request):
        return json({})

    with pytest.raises(RouteExists, match="Route already registered"):

        @app.post("/import")
        async def import_plain(request):
            return text("")


def test_negotiated_route_cannot_overload_plain():
    app = Sanic("mixed-reverse")

    @app.post("/import")
    async def import_plain(request):
        return text("")

    with pytest.raises(RouteExists, match="Route already registered"):

        @app.post("/import", consumes=JSON)
        async def import_json(request):
            return json({})


def test_invalid_media_type_declaration():
    app = Sanic("invalid-media")

    with pytest.raises(ValueError, match="Invalid media type"):

        @app.post("/import", consumes="not-a-media-type")
        async def import_json(request):
            return json({})
