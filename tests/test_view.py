import json

import httpx
import pytest

from renderdesk import tools
from renderdesk.app import app
from renderdesk.db import session_scope

from .conftest import make_connection


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as c:
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
    # This wrapper page is the one place that actually embeds an iframe of
    # its own /raw content, so it's the one place frame-src 'self' belongs.
    assert "frame-src 'self'" in page_resp.headers["content-security-policy"]


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


async def test_code_artifact_is_syntax_highlighted(client):
    connection_id = await make_connection()
    code = "def foo(x):\n    return x + 1\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, code, "code", "foo.py", language="python")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert '<pre class="codehilite">' in resp.text
    assert '<span class="k">def</span>' in resp.text
    assert ".codehilite{" in resp.text
    assert "/static/theme.js" in resp.text  # follows the dashboard's light/dark theme
    # Checked directive-by-directive (not an exact string) since this page
    # never embeds an iframe, unlike the plain-HTML wrapper page, so it
    # shouldn't need frame-src at all. script-src 'self' is same-origin-only
    # (theme.js) — no other loosening beyond that.
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "img-src data: blob:" in csp
    assert "frame-src" not in csp
    assert "script-src 'self'" in csp

    raw_resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert raw_resp.status_code == 200
    assert raw_resp.text == code
    assert raw_resp.headers["content-type"].startswith("text/plain")
    # No route-specific CSP is set here (text/plain never executes) — only
    # SecurityHeadersMiddleware's baseline default lands.
    assert raw_resp.headers["content-security-policy"] == "frame-ancestors 'none'"


async def test_code_artifact_without_language_falls_back_to_plain_text(client):
    connection_id = await make_connection()
    code = "just some text, no language given\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, code, "code")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "codehilite" not in resp.text
    assert "just some text, no language given" in resp.text


async def test_code_artifact_with_unrecognized_language_falls_back_to_plain_text(client):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(
            session, connection_id, "hello", "code", language="not-a-real-language"
        )

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "codehilite" not in resp.text
    assert "hello" in resp.text


async def test_code_artifact_cannot_inject_script_via_language_or_content(client):
    connection_id = await make_connection()
    malicious = "</pre><script>alert(1)</script>"
    async with session_scope() as session:
        published = await tools.publish_artifact(
            session, connection_id, malicious, "code", language=malicious
        )

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text

    raw_resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert raw_resp.text == malicious  # raw is exact and unrendered — text/plain never executes


async def test_csv_artifact_is_rendered_as_a_table(client):
    connection_id = await make_connection()
    content = "name,age\nAda,36\nGrace,85\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, content, "csv", "people.csv")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert '<table class="csv-table">' in resp.text
    assert "<thead><tr><th>name</th><th>age</th></tr></thead>" in resp.text
    assert "<td>Ada</td><td>36</td>" in resp.text
    assert "<td>Grace</td><td>85</td>" in resp.text
    assert "/static/csv-table-init.js" in resp.text
    assert "/static/theme.js" in resp.text  # follows the dashboard's light/dark theme
    assert 'html[data-theme="dark"]' in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "default-src 'none'" in csp

    raw_resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert raw_resp.status_code == 200
    assert raw_resp.text == content
    assert raw_resp.headers["content-type"].startswith("text/csv")
    # No route-specific CSP is set here (text/csv never executes) — only
    # SecurityHeadersMiddleware's baseline default lands.
    assert raw_resp.headers["content-security-policy"] == "frame-ancestors 'none'"


async def test_csv_artifact_cannot_inject_script_via_content(client):
    connection_id = await make_connection()
    malicious = 'name,bio\n"</table><script>alert(1)</script>",hi\n'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, malicious, "csv")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text

    raw_resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert raw_resp.text == malicious  # raw is exact and unrendered — text/csv never executes


async def test_empty_csv_artifact_renders_without_error(client):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "", "csv")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "(empty)" in resp.text


async def test_html_wrapper_page_also_follows_dashboard_theme(client):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "<h1>hi</h1>", "html")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "/static/theme.js" in resp.text
    assert 'html[data-theme="dark"]' in resp.text
    assert "script-src 'self'" in resp.headers["content-security-policy"]


async def test_code_artifact_has_light_and_dark_pygments_styles(client):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(
            session, connection_id, "def foo():\n    pass\n", "code", language="python"
        )

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    # Both style rulesets are always present, scoped by data-theme purely
    # via CSS — no re-highlighting needed client-side to switch.
    assert '.codehilite .k { color:' in resp.text  # light ("friendly"), unscoped
    assert 'html[data-theme="dark"] .codehilite .k { color:' in resp.text  # dark ("monokai")


async def test_markdown_raw_route_still_404s(client):
    connection_id = await make_connection()
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, "# hi", "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 404


async def test_markdown_mermaid_fence_renders_as_diagram_with_relaxed_csp(client):
    connection_id = await make_connection()
    markdown = "# Diagram\n\n```mermaid\ngraph TD;\nA-->B;\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert '<pre class="mermaid">graph TD;\nA--&gt;B;\n</pre>' in resp.text
    assert "mermaid-init.js" in resp.text
    assert "script-src 'self'" in resp.headers["content-security-policy"]


async def test_markdown_without_mermaid_does_not_load_mermaid(client):
    connection_id = await make_connection()
    markdown = "# No diagrams here\n\n```python\nprint('hi')\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "mermaid-init.js" not in resp.text
    # theme.js (light/dark tokens) is the one script every artifact-render
    # page loads regardless of format — script-src 'self' covers only that.
    assert "/static/theme.js" in resp.text
    assert "script-src 'self'" in resp.headers["content-security-policy"]


async def test_mermaid_fence_cannot_inject_script_via_diagram_content(client):
    connection_id = await make_connection()
    # A crafted "diagram" trying to break out of the <pre> and inject script.
    markdown = "```mermaid\n</pre><script>alert(1)</script>\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text
    assert "alert(1)" in resp.text  # present as inert, escaped text — not executable

    vendor_resp = await client.get("/static/vendor/mermaid-11.16.0.min.js")
    assert vendor_resp.status_code == 200


async def test_html_artifact_with_mermaid_class_gets_runtime_injected(client):
    connection_id = await make_connection()
    html = "<html><body><h1>Diagram</h1><pre class=\"mermaid\">graph TD;A-->B;</pre></body></html>"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "mermaid-init.js" in resp.text
    assert 'script-src \'unsafe-inline\' \'unsafe-eval\' \'self\';' in resp.headers["content-security-policy"]
    # Injected right before </body>, original content otherwise untouched.
    assert resp.text.replace(
        '<script src="/static/vendor/mermaid-11.16.0.min.js"></script>'
        '<script src="/static/mermaid-init.js"></script>',
        "",
    ) == html


async def test_markdown_code_fence_is_syntax_highlighted(client):
    connection_id = await make_connection()
    markdown = "# Code\n\n```python\ndef foo(x):\n    return x + 1\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert '<pre class="codehilite">' in resp.text
    assert '<span class="k">def</span>' in resp.text
    assert ".codehilite{" in resp.text  # highlight CSS injected
    # Highlighting itself is server-side, no CSP loosening beyond theme.js
    # (loaded on every artifact-render page — see test_markdown_without_mermaid_does_not_load_mermaid).
    assert "script-src 'self'" in resp.headers["content-security-policy"]


async def test_markdown_fence_with_unknown_language_falls_back_to_plain_text(client):
    connection_id = await make_connection()
    markdown = "```not-a-real-language\nhello\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "codehilite" not in resp.text
    assert "hello" in resp.text


async def test_code_fence_cannot_inject_script_via_language_string(client):
    connection_id = await make_connection()
    # A crafted "language" trying to break out via Pygments lexer lookup.
    markdown = "```</code></pre><script>alert(1)</script>\nprint(1)\n```\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text


async def test_markdown_math_triggers_katex_assets_and_relaxed_csp(client):
    connection_id = await make_connection()
    markdown = "Einstein: $E = mc^2$\n\n$$\\int_0^1 x^2 dx$$\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert '<span class="math inline">E = mc^2</span>' in resp.text
    assert '<div class="math block">' in resp.text
    assert "katex-init.js" in resp.text
    assert "katex.min.css" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "style-src 'unsafe-inline' 'self'" in csp
    assert "font-src 'self'" in csp


async def test_math_block_preserves_double_backslash_line_breaks(client):
    connection_id = await make_connection()
    # Regression test: a LaTeX matrix's \\ line break was previously
    # collapsed to a single \ by CommonMark's generic backslash-escape
    # rule, since math content had no dedicated parsing of its own and
    # was treated as ordinary paragraph text.
    markdown = "$$\n\\begin{pmatrix}\na & b \\\\\nc & d\n\\end{pmatrix}\n$$\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "a &amp; b \\\\" in resp.text
    assert "a &amp; b \\\n" not in resp.text  # would indicate the \\ got collapsed to \


async def test_math_content_cannot_inject_script(client):
    connection_id = await make_connection()
    markdown = "$$\n</div><script>alert(1)</script>\n$$\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "<script>" not in resp.text


async def test_markdown_prose_with_stray_dollar_sign_does_not_trigger_katex(client):
    connection_id = await make_connection()
    markdown = "This costs $5 and that costs $10.\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "katex-init.js" not in resp.text
    assert "font-src" not in resp.headers["content-security-policy"]


async def test_markdown_with_mermaid_and_math_gets_union_of_relaxed_csp(client):
    connection_id = await make_connection()
    markdown = "```mermaid\ngraph TD;\nA-->B;\n```\n\n$$x^2$$\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, markdown, "markdown")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "mermaid-init.js" in resp.text
    assert "katex-init.js" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "font-src 'self'" in csp


async def test_html_artifact_without_mermaid_class_is_unaffected(client):
    connection_id = await make_connection()
    html = "<h1>hello</h1><script>alert(1)</script>"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert resp.text == html  # untouched, byte-for-byte
    assert "mermaid" not in resp.text
    # Directive-by-directive rather than an exact string, so adding an
    # unrelated directive elsewhere doesn't break this assertion.
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "sandbox allow-scripts" in csp
    assert "script-src 'unsafe-inline' 'unsafe-eval'" in csp
    assert "style-src 'unsafe-inline'" in csp
    assert "img-src data: blob:" in csp
    assert "font-src data:" in csp
    assert "connect-src 'none'" in csp
    assert "frame-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp


async def test_react_artifact_view_page_embeds_sandboxed_iframe(client):
    connection_id = await make_connection()
    jsx = "export default function App() { return <h1>Hi</h1>; }\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react", "My Component")

    resp = await client.get(f"/a/{published['artifact_id']}")
    assert resp.status_code == 200
    assert "iframe" in resp.text
    assert f"/a/{published['artifact_id']}/raw" in resp.text
    # Same iframe-wrapper CSP treatment as html format — see
    # test_html_artifact_served_byte_for_byte_with_csp.
    assert "frame-src 'self'" in resp.headers["content-security-policy"]


async def test_react_artifact_raw_route_wraps_source_for_babel(client):
    connection_id = await make_connection()
    jsx = "export default function App() { return <h1>Hi</h1>; }\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert '<div id="root"></div>' in resp.text
    assert "/static/vendor/react-18.3.1.production.min.js" in resp.text
    assert "/static/vendor/react-dom-18.3.1.production.min.js" in resp.text
    assert "/static/vendor/babel-standalone-7.26.9.min.js" in resp.text
    assert "/static/react-init.js" in resp.text

    embedded = resp.text.split('<script id="artifact-source" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded) == jsx

    csp = resp.headers["content-security-policy"]
    # script-src needs no 'unsafe-inline' (unlike html format) — every
    # <script> tag on this page is one of ours, the artifact source is inert
    # JSON text. style-src keeps 'unsafe-inline' (components may render a
    # literal <style> tag), so this checks script-src specifically.
    assert "script-src 'self' 'unsafe-eval'" in csp
    assert "sandbox allow-scripts" in csp
    assert "connect-src 'none'" in csp


async def test_react_artifact_source_cannot_break_out_of_script_tag(client):
    connection_id = await make_connection()
    malicious = 'export default function App() { return <div>{"</script><script>alert(1)</script>"}</div>; }\n'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, malicious, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    embedded = resp.text.split('<script id="artifact-source" type="application/json">')[1].split("</script>")[0]
    assert json.loads(embedded) == malicious
    assert "<script>alert(1)</script>" not in resp.text  # would indicate a broken-out tag


_REACT_OPTIONAL_LIBRARY_CASES = [
    ('import * as THREE from "three";', "/static/vendor/three-0.160.0.min.js"),
    ('import _ from "lodash";', "/static/vendor/lodash-4.18.1.min.js"),
    ('import * as d3 from "d3";', "/static/vendor/d3-7.9.0.min.js"),
    ('import * as math from "mathjs";', "/static/vendor/mathjs-15.2.0.js"),
    ('import { Chart } from "chart.js";', "/static/vendor/chart.js-4.5.1.min.js"),
    ('import "chart.js/auto";', "/static/vendor/chart.js-4.5.1.min.js"),
    ('import * as Tone from "tone";', "/static/vendor/tone-15.1.22.js"),
    ('import Papa from "papaparse";', "/static/vendor/papaparse-5.6.0.min.js"),
    ('import * as XLSX from "xlsx";', "/static/vendor/xlsx-0.20.3.full.min.js"),
    ('const _ = require("lodash");', "/static/vendor/lodash-4.18.1.min.js"),
]


@pytest.mark.parametrize(("import_line", "vendor_path"), _REACT_OPTIONAL_LIBRARY_CASES)
async def test_react_artifact_import_loads_matching_vendored_library(client, import_line, vendor_path):
    connection_id = await make_connection()
    jsx = f'{import_line}\nexport default function App() {{ return <h1>Hi</h1>; }}\n'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert vendor_path in resp.text


async def test_react_artifact_without_optional_imports_loads_no_optional_libraries(client):
    connection_id = await make_connection()
    jsx = "export default function App() { return <h1>Hi</h1>; }\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    for _, vendor_path in _REACT_OPTIONAL_LIBRARY_CASES:
        assert vendor_path not in resp.text


async def test_react_artifact_bare_substring_does_not_load_library(client):
    connection_id = await make_connection()
    # "three" and "d3" appear here only as ordinary identifiers/prose, never
    # as an import/require specifier — must not trigger loading.
    jsx = (
        "const three = 3;\n"
        "// d3.js would be neat for this, but we're not using it\n"
        "export default function App() { return <h1>{three}</h1>; }\n"
    )
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "/static/vendor/three-0.160.0.min.js" not in resp.text
    assert "/static/vendor/d3-7.9.0.min.js" not in resp.text


async def test_react_artifact_with_bootstrap_icons_class_gets_stylesheet_and_relaxed_csp(client):
    connection_id = await make_connection()
    jsx = 'export default function App() { return <i className="bi bi-camera"></i>; }\n'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "/static/vendor/bootstrap-icons/bootstrap-icons-1.13.1.min.css" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline' 'self'" in csp
    assert "font-src data: 'self'" in csp
    # script-src is already 'self'-scoped for every react response — icons
    # shouldn't change that.
    assert "script-src 'self' 'unsafe-eval'" in csp


async def test_react_artifact_without_bootstrap_icons_class_is_unaffected(client):
    connection_id = await make_connection()
    jsx = "export default function App() { return <h1>Hi</h1>; }\n"
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, jsx, "react")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "bootstrap-icons" not in resp.text
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline';" in csp
    assert "font-src data:;" in csp


async def test_html_artifact_cdnjs_script_is_rewritten_to_vendored_equivalent(client):
    connection_id = await make_connection()
    html = (
        "<html><body>"
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
        "</body></html>"
    )
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "cdnjs.cloudflare.com" not in resp.text
    assert '<script src="/static/vendor/d3-7.9.0.min.js"></script>' in resp.text
    assert 'script-src \'unsafe-inline\' \'unsafe-eval\' \'self\';' in resp.headers["content-security-policy"]


async def test_html_artifact_cdnjs_rewrite_ignores_version_in_url(client):
    connection_id = await make_connection()
    # A different version number in the URL than the one renderdesk vendors
    # still matches — the rewrite keys on the library slug, not the version.
    html = '<script src="https://cdnjs.cloudflare.com/ajax/libs/lodash.js/4.17.15/lodash.min.js"></script>'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert '<script src="/static/vendor/lodash-4.18.1.min.js"></script>' in resp.text


async def test_html_artifact_unknown_cdnjs_library_is_left_untouched(client):
    connection_id = await make_connection()
    html = '<script src="https://cdnjs.cloudflare.com/ajax/libs/jquery/3.7.1/jquery.min.js"></script>'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert resp.text == html  # byte-for-byte untouched — jquery isn't vendored
    csp = resp.headers["content-security-policy"]
    assert "script-src 'unsafe-inline' 'unsafe-eval';" in csp  # not widened


async def test_html_artifact_with_mermaid_and_cdnjs_rewrite_gets_single_csp_variant(client):
    connection_id = await make_connection()
    html = (
        '<pre class="mermaid">graph TD;A-->B;</pre>'
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
    )
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    # script-src gains exactly one 'self' (mermaid + the cdnjs rewrite both
    # firing shouldn't add it twice) — the base CSP's unrelated
    # frame-ancestors 'self' accounts for the other occurrence.
    assert 'script-src \'unsafe-inline\' \'unsafe-eval\' \'self\';' in csp
    assert csp.count("'self'") == 2


async def test_html_artifact_with_bootstrap_icons_class_gets_stylesheet_and_relaxed_csp(client):
    connection_id = await make_connection()
    html = '<i class="bi bi-camera"></i>'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "/static/vendor/bootstrap-icons/bootstrap-icons-1.13.1.min.css" in resp.text
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline' 'self'" in csp
    assert "font-src data: 'self'" in csp
    assert "script-src 'unsafe-inline' 'unsafe-eval';" in csp  # icons alone don't touch script-src


@pytest.mark.parametrize(
    "vendor_path",
    [
        "/static/vendor/three-0.160.0.min.js",
        "/static/vendor/lodash-4.18.1.min.js",
        "/static/vendor/d3-7.9.0.min.js",
        "/static/vendor/mathjs-15.2.0.js",
        "/static/vendor/chart.js-4.5.1.min.js",
        "/static/vendor/tone-15.1.22.js",
        "/static/vendor/papaparse-5.6.0.min.js",
        "/static/vendor/xlsx-0.20.3.full.min.js",
        "/static/vendor/bootstrap-icons/bootstrap-icons-1.13.1.min.css",
        "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
    ],
)
async def test_vendored_optional_library_file_exists(client, vendor_path):
    resp = await client.get(vendor_path)
    assert resp.status_code == 200


async def test_mermaid_class_inside_html_comment_does_not_trigger_injection(client):
    connection_id = await make_connection()
    # Regression test: an artifact merely *documenting* the convention in a
    # comment — e.g. explaining why it deliberately isn't using a feature —
    # previously matched the same way a real `class="mermaid"` element
    # would, loading mermaid.min.js (~3.5MB) and widening the CSP for
    # nothing. Found via a real production artifact that hit exactly this.
    html = '<!-- note: a class="mermaid" element would draw a diagram here --><body></body>'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert resp.text == html  # byte-for-byte untouched, no runtime spliced in
    assert "mermaid-init.js" not in resp.text
    csp = resp.headers["content-security-policy"]
    assert "script-src 'unsafe-inline' 'unsafe-eval';" in csp  # not widened


async def test_bootstrap_icons_class_inside_html_comment_does_not_trigger_injection(client):
    connection_id = await make_connection()
    html = '<!-- example: class="bi bi-camera" renders a camera icon --><body></body>'
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "bootstrap-icons" not in resp.text
    csp = resp.headers["content-security-policy"]
    assert "style-src 'unsafe-inline';" in csp  # not widened
    assert "font-src data:;" in csp


async def test_cdnjs_script_inside_html_comment_is_not_rewritten(client):
    connection_id = await make_connection()
    html = (
        "<!-- e.g. "
        '<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>'
        " would load D3 --><body></body>"
    )
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert resp.text == html  # byte-for-byte untouched, including inside the comment
    csp = resp.headers["content-security-policy"]
    assert "script-src 'unsafe-inline' 'unsafe-eval';" in csp  # not widened


async def test_mermaid_class_outside_comment_still_triggers_after_unrelated_comment(client):
    connection_id = await make_connection()
    # A comment mentioning the feature shouldn't suppress detection of a
    # *real* element elsewhere in the same artifact.
    html = (
        '<!-- class="mermaid" is how you\'d draw a diagram -->'
        '<pre class="mermaid">graph TD;A-->B;</pre>'
    )
    async with session_scope() as session:
        published = await tools.publish_artifact(session, connection_id, html, "html")

    resp = await client.get(f"/a/{published['artifact_id']}/raw")
    assert resp.status_code == 200
    assert "mermaid-init.js" in resp.text

