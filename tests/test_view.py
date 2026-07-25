import httpx
import pytest

from renderdesk import tools
from renderdesk.app import app
from renderdesk.db import session_scope

from .conftest import make_connection


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_html_artifact_served_byte_for_byte_with_csp(client):
    connection_id = await make_connection()
    html = "<h1>hello</h1><script>alert(1)</script>"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html", "My Page")

    raw_resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert raw_resp.status_code == 200
    assert raw_resp.text == html
    assert "connect-src 'none'" in raw_resp.headers["content-security-policy"]
    assert "object-src 'none'" in raw_resp.headers["content-security-policy"]

    page_resp = await client.get(f"/a/{published['artifact_id']}")
    assert page_resp.status_code == 200
    assert "iframe" in page_resp.text
    assert f"/a/{published['artifact_id']}/raw" in page_resp.text


async def test_markdown_artifact_is_sanitized(client):
    connection_id = await make_connection()
    markdown = "# Title\n\n<script>alert(1)</script>\n\nSome *text*."
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "<h1>Title</h1>" in resp.text
    assert "<em>text</em>" in resp.text


async def test_unknown_artifact_id_is_404(client):
    resp = await client.get("/a/does-not-exist")
    assert resp.status_code == 404
