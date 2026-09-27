import asyncio

import pytest

from sanic_routing.exceptions import RouteExists

from sanic import Sanic
from sanic.blueprints import Blueprint
from sanic.response import text


NFC_FORM = "café"  # "café" as U+00E9
NFD_FORM = "café"  # "café" as "e" + U+0301
NFC_ENCODED = "/caf%C3%A9"
NFD_ENCODED = "/cafe%CC%81"


@pytest.fixture
def nfc_app():
    app = Sanic("nfc_paths")
    app.config.NFC_PATHS = True

    @app.get(f"/{NFC_FORM}")
    async def cafe(request):
        return text("cafe-handler")

    @app.get("/user/<name>")
    async def user(request, name):
        return text(name)

    @app.post(f"/submit/{NFC_FORM}")
    async def submit(request):
        return text("posted")

    @app.get("/files/<name>")
    async def files(request, name):
        return text(name)

    @app.get("/files/a/b")
    async def files_ab(request):
        return text("static-ab")

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", [NFC_ENCODED, NFD_ENCODED, f"/{NFC_FORM}", f"/{NFD_FORM}"]
)
async def test_equivalent_forms_hit_same_route(nfc_app, path):
    _, response = await nfc_app.asgi_client.get(path)
    assert response.status == 200
    assert response.text == "cafe-handler"
    # served directly, never redirected
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_no_redirect_for_any_method(nfc_app):
    _, response = await nfc_app.asgi_client.post(f"/submit{NFD_ENCODED}")
    assert response.status == 200
    assert response.text == "posted"
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_combining_mark_param_decoded(nfc_app):
    _, response = await nfc_app.asgi_client.get(f"/user{NFD_ENCODED}")
    assert response.status == 200
    assert response.text == NFC_FORM


@pytest.mark.asyncio
async def test_match_info_is_decoded(nfc_app):
    @nfc_app.get("/item/<name>")
    async def item(request, name):
        return text(request.match_info["name"])

    _, response = await nfc_app.asgi_client.get(f"/item{NFD_ENCODED}")
    assert response.text == NFC_FORM


@pytest.mark.asyncio
async def test_invalid_utf8_static_segment_404s(nfc_app):
    _, response = await nfc_app.asgi_client.get("/caf%C3")
    assert response.status == 404


@pytest.mark.asyncio
async def test_invalid_utf8_param_left_encoded(nfc_app):
    _, response = await nfc_app.asgi_client.get("/user/caf%C3")
    assert response.status == 200
    assert response.text == "caf%C3"


@pytest.mark.asyncio
async def test_encoded_slash_does_not_create_hierarchy(nfc_app):
    # %2F must match the single-segment param route, not /files/a/b
    _, response = await nfc_app.asgi_client.get("/files/a%2Fb")
    assert response.status == 200
    assert response.text == "a/b"

    _, response = await nfc_app.asgi_client.get("/files/a/b")
    assert response.status == 200
    assert response.text == "static-ab"


@pytest.mark.asyncio
async def test_encoded_backslash_stays_in_segment(nfc_app):
    _, response = await nfc_app.asgi_client.get("/files/a%5Cb")
    assert response.status == 200
    assert response.text == "a\\b"


@pytest.mark.asyncio
async def test_double_encoding_decoded_exactly_once(nfc_app):
    _, response = await nfc_app.asgi_client.get("/files/a%252Fb")
    assert response.status == 200
    assert response.text == "a%2Fb"


def test_static_conflict_raises():
    app = Sanic("nfc_conflict")
    app.config.NFC_PATHS = True

    @app.get(f"/{NFC_FORM}")
    async def cafe_nfc(request):
        return text("nfc")

    message = "Route already registered"
    with pytest.raises(RouteExists, match=message):

        @app.get(f"/{NFD_FORM}")
        async def cafe_nfd(request):
            return text("nfd")


def test_url_for_generates_canonical_form():
    app = Sanic("nfc_url_for")
    app.config.NFC_PATHS = True

    @app.get(f"/{NFD_FORM}")
    async def cafe(request):
        return text("cafe")

    @app.get("/user/<name>")
    async def user(request, name):
        return text(name)

    assert app.url_for("cafe") == NFC_ENCODED
    assert app.url_for("user", name=NFD_FORM) == f"/user/{NFC_FORM}"


@pytest.mark.asyncio
async def test_blueprint_prefix_normalized():
    app = Sanic("nfc_blueprint")
    app.config.NFC_PATHS = True

    bp = Blueprint("bp", url_prefix=f"/{NFD_FORM}")

    @bp.get("/info")
    async def info(request):
        return text("bp-info")

    app.blueprint(bp)

    async with app.asgi_client as client:
        for path in (f"{NFC_ENCODED}/info", f"{NFD_ENCODED}/info"):
            _, response = await client.get(path)
            assert response.status == 200
            assert response.text == "bp-info"

    assert app.url_for("bp.info") == f"{NFC_ENCODED}/info"


def test_normal_mode_unchanged():
    # Without NFC_PATHS, equivalent forms remain distinct routes and
    # parameters stay percent-encoded.
    app = Sanic("normal_mode")

    @app.get(f"/{NFC_FORM}")
    async def cafe_nfc(request):
        return text("nfc")

    @app.get(f"/{NFD_FORM}")
    async def cafe_nfd(request):
        return text("nfd")

    @app.get("/user/<name>")
    async def user(request, name):
        return text(name)

    app.finalize()

    async def run():
        async with app.asgi_client as client:
            _, r1 = await client.get(NFC_ENCODED)
            _, r2 = await client.get(NFD_ENCODED)
            _, r3 = await client.get(f"/user{NFC_ENCODED}")
            return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    assert r1.text == "nfc"
    assert r2.text == "nfd"
    assert r3.text == "caf%C3%A9"
    assert app.url_for("cafe_nfc") == NFC_ENCODED
    assert app.url_for("cafe_nfd") == NFD_ENCODED
