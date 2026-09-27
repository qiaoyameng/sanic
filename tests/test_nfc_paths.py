import pytest

from sanic import Blueprint, Sanic
from sanic.exceptions import SanicException
from sanic.response import text
from sanic.router import normalize_nfc_path


NFC_CAFE = "/café"
NFD_CAFE = "/café"
ENC_NFC_CAFE = "/caf%C3%A9"
ENC_NFD_CAFE = "/cafe%CC%81"
NFC_E = "é"
NFD_E = "é"


@pytest.fixture
def nfc_app():
    app = Sanic("nfc_app")
    app.config.NFC_PATHS = True

    @app.get(NFC_CAFE, name="cafe")
    async def cafe(request):
        return text("cafe")

    @app.route("/user/<name>", methods=["GET", "POST"], name="user")
    async def user(request, name):
        return text(f"{request.method}:{name}")

    @app.get("/unquote/<name>", unquote=True, name="unquote_user")
    async def unquote_user(request, name):
        return text(name)

    @app.get("/a/b", name="ab")
    async def ab(request):
        return text("a/b")

    return app


def test_normalize_nfc_path_static():
    assert normalize_nfc_path(ENC_NFC_CAFE) == ENC_NFC_CAFE
    assert normalize_nfc_path(ENC_NFD_CAFE) == ENC_NFC_CAFE
    assert normalize_nfc_path("/plain/ascii") == "/plain/ascii"


def test_normalize_nfc_path_does_not_create_path_levels():
    # %2F, %5C and double-encoded separators must never decode
    # into new path segments
    assert normalize_nfc_path("/x/a%2Fb") == "/x/a%2Fb"
    assert normalize_nfc_path("/x/a%5Cb") == "/x/a%5Cb"
    assert normalize_nfc_path("/x/a%252Fb") == "/x/a%252Fb"
    assert normalize_nfc_path("/x/%2F") == "/x/%2F"


def test_normalize_nfc_path_invalid_utf8():
    # invalid UTF-8 becomes the replacement char, it never raises
    assert normalize_nfc_path("/x/%C3%28") == "/x/%EF%BF%BD%28"


@pytest.mark.asyncio
async def test_static_route_matches_equivalent_unicode(nfc_app):
    async with nfc_app.asgi_client as client:
        for path in (ENC_NFC_CAFE, ENC_NFD_CAFE, NFD_CAFE, NFC_CAFE):
            _, response = await client.get(path)
            assert response.status_code == 200
            assert response.text == "cafe"


@pytest.mark.asyncio
async def test_encoded_separator_is_not_a_path_level(nfc_app):
    async with nfc_app.asgi_client as client:
        _, response = await client.get("/a%2Fb")
        assert response.status_code == 404
        _, response = await client.get("/a%5Cb")
        assert response.status_code == 404
        _, response = await client.get("/a%252Fb")
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_combining_diacritic_param_canonicalized(nfc_app):
    async with nfc_app.asgi_client as client:
        _, response = await client.get("/user/e%CC%81")
        assert response.status_code == 200
        # param is delivered in canonical NFC encoded form
        assert response.text == "GET:%C3%A9"

        _, response = await client.get("/user/%C3%A9")
        assert response.text == "GET:%C3%A9"


@pytest.mark.asyncio
async def test_combining_diacritic_param_unquote_route(nfc_app):
    async with nfc_app.asgi_client as client:
        _, response = await client.get("/unquote/e%CC%81")
        assert response.status_code == 200
        assert response.text == NFC_E


@pytest.mark.asyncio
async def test_invalid_utf8_param_is_interpretable(nfc_app):
    async with nfc_app.asgi_client as client:
        _, response = await client.get("/user/%C3%28")
        assert response.status_code == 200
        assert response.text == "GET:%EF%BF%BD%28"

        _, response = await client.get("/unquote/%C3%28")
        assert response.status_code == 200
        assert response.text == "�("


@pytest.mark.asyncio
async def test_encoded_separator_param_kept_raw(nfc_app):
    async with nfc_app.asgi_client as client:
        _, response = await client.get("/user/a%2Fb")
        assert response.status_code == 200
        assert response.text == "GET:a%2Fb"

        _, response = await client.get("/user/a%5Cb")
        assert response.text == "GET:a%5Cb"

        _, response = await client.get("/user/a%252Fb")
        assert response.text == "GET:a%252Fb"


@pytest.mark.asyncio
async def test_method_preserved_without_redirect(nfc_app):
    async with nfc_app.asgi_client as client:
        # POST to an NFD path is routed directly: no 3xx redirect
        # and no method rewriting
        _, response = await client.post("/user/e%CC%81")
        assert response.status_code == 200
        assert response.text == "POST:%C3%A9"


@pytest.mark.asyncio
async def test_blueprint_prefix_equivalent_unicode():
    app = Sanic("nfc_bp")
    app.config.NFC_PATHS = True

    bp = Blueprint("bp", url_prefix=NFD_CAFE)

    @bp.get("/x")
    async def x(request):
        return text("bp-x")

    app.blueprint(bp)

    async with app.asgi_client as client:
        for path in (f"{ENC_NFC_CAFE}/x", f"{ENC_NFD_CAFE}/x"):
            _, response = await client.get(path)
            assert response.status_code == 200
            assert response.text == "bp-x"

    assert app.url_for("bp.x") == f"{ENC_NFC_CAFE}/x"


@pytest.mark.asyncio
async def test_static_route_conflict_raises():
    app = Sanic("nfc_conflict")
    app.config.NFC_PATHS = True

    @app.get(NFC_CAFE)
    async def cafe_nfc(request):
        return text("nfc")

    message = "Route collision after NFC normalization"
    with pytest.raises(SanicException, match=message):

        @app.get(NFD_CAFE)
        async def cafe_nfd(request):
            return text("nfd")

    # the original route is still intact and was not overridden
    async with app.asgi_client as client:
        _, response = await client.get(ENC_NFC_CAFE)
        assert response.status_code == 200
        assert response.text == "nfc"


def test_url_for_generates_canonical_form():
    app = Sanic("nfc_url_for")
    app.config.NFC_PATHS = True

    @app.get(NFD_CAFE, name="cafe")
    async def cafe(request):
        return text("cafe")

    @app.get("/user/<name>", name="user")
    async def user(request, name):
        return text(name)

    # a route registered in NFD is built in canonical NFC form
    assert app.url_for("cafe") == ENC_NFC_CAFE
    # parameter values are normalized to NFC as well
    assert app.url_for("user", name=NFD_E) == f"/user/{NFC_E}"


@pytest.mark.asyncio
async def test_normal_mode_behavior_unchanged():
    app = Sanic("normal_mode")

    @app.get(NFC_CAFE, name="cafe")
    async def cafe(request):
        return text("cafe")

    @app.get("/user/<name>", name="user")
    async def user(request, name):
        return text(name)

    async with app.asgi_client as client:
        _, response = await client.get(ENC_NFC_CAFE)
        assert response.status_code == 200

        # without NFC_PATHS, the NFD spelling is a different route
        _, response = await client.get(ENC_NFD_CAFE)
        assert response.status_code == 404

        # params stay exactly as encoded by the client
        _, response = await client.get("/user/e%CC%81")
        assert response.status_code == 200
        assert response.text == "e%CC%81"

    # url_for is untouched
    assert app.url_for("user", name=NFD_E) == f"/user/{NFD_E}"
