import httpx
import pytest

from renderdesk.app import app

# The vendored Bootstrap Icons webfont: the one asset that actually needs
# CORS, because html/react artifacts load it from a sandboxed opaque-origin
# iframe. See static_files.CORSStaticFiles for the full reasoning.
_FONT = "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2"
_CSS = "/static/vendor/bootstrap-icons/bootstrap-icons-1.13.1.min.css"


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
        yield c


async def test_icon_webfont_is_fetchable_from_a_sandboxed_opaque_origin(client):
    # `Origin: null` is what a sandbox="allow-scripts" iframe sends. Without
    # the ACAO header the browser blocks the font and every bi-* icon
    # renders as its raw Private Use Area codepoint instead of a glyph.
    resp = await client.get(_FONT, headers={"Origin": "null"})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"
    # A real woff2, not an HTML error page.
    assert resp.content[:4] == b"wOF2"


async def test_static_assets_carry_cors_headers_generally(client):
    # Applied to the whole mount rather than sniffed per extension, so the
    # next vendored font doesn't silently repeat the bug.
    resp = await client.get(_CSS)

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


async def test_cors_grant_does_not_extend_credentials(client):
    # `*` is only safe because no cookies ride along; Allow-Credentials must
    # stay absent (and is incompatible with `*` anyway).
    resp = await client.get(_FONT, headers={"Origin": "null"})

    assert "access-control-allow-credentials" not in resp.headers


async def test_dashboard_routes_are_not_made_cross_origin_readable(client):
    # The CORS grant is scoped to the /static mount — nothing else.
    resp = await client.get("/dashboard/login", headers={"Origin": "null"})

    assert "access-control-allow-origin" not in resp.headers
