import csv
import html as html_escape
import io
import re
from collections.abc import Sequence

import nh3
from fastapi import APIRouter, HTTPException, Response
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.token import STANDARD_TYPES
from pygments.util import ClassNotFound
from sqlalchemy import select

from renderdesk.db import session_scope
from renderdesk.models import Artifact, ArtifactFormat

router = APIRouter()

_HTML_CSP = (
    "default-src 'none'; "
    # Without this, a link straight to /a/{id}/raw (bypassing the sandboxed
    # iframe in view_artifact) would run artifact JS as a normal top-level
    # document in our real origin, with access to cookies and same-origin
    # requests. The CSP sandbox directive forces the same opaque-origin,
    # no-cookie-access restrictions here as the iframe's sandbox="allow-scripts"
    # attribute already applies when this is loaded embedded.
    "sandbox allow-scripts; "
    "script-src 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'self'"
)
# 'self' (same-origin — our own vendored mermaid.min.js, never an external
# CDN) is only added when the artifact actually contains a mermaid marker;
# see _inject_mermaid_if_present.
_HTML_CSP_WITH_MERMAID = _HTML_CSP.replace(
    "script-src 'unsafe-inline' 'unsafe-eval';", "script-src 'unsafe-inline' 'unsafe-eval' 'self';"
)

_MERMAID_VERSION = "11.16.0"
_MERMAID_ASSETS = (
    f'<script src="/static/vendor/mermaid-{_MERMAID_VERSION}.min.js"></script>'
    '<script src="/static/mermaid-init.js"></script>'
)

_KATEX_ASSETS = (
    '<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">'
    '<script src="/static/vendor/katex/katex.min.js"></script>'
    '<script src="/static/katex-init.js"></script>'
)

# Same light/dark token values as static/styles.css's :root / [data-theme=dark]
# — duplicated rather than linked, matching this project's existing "each
# artifact-render page is small and self-contained" pattern (see mermaid/
# katex, both fully vendored rather than shared). Only the subset these
# monospace-flavored pages actually use. theme.js (same script, unchanged)
# resolves data-theme from localStorage/system preference and must run
# before anything that reads the attribute — mermaid-init.js in particular.
_THEME_TOKENS_CSS = (
    "<style>:root{--font-mono:ui-monospace,'SF Mono',Menlo,Consolas,monospace;"
    "--bg:oklch(97% 0.008 95);--surface-2:oklch(94.5% 0.012 95);--border:oklch(88% 0.014 95);"
    "--text:oklch(24% 0.02 95);--text-muted:oklch(48% 0.02 95);"
    "--accent:oklch(72% 0.15 155);--accent-strong:oklch(58% 0.15 155);color-scheme:light}"
    'html[data-theme="dark"]{--bg:oklch(19% 0.015 95);--surface-2:oklch(28.5% 0.018 95);'
    "--border:oklch(33% 0.02 95);--text:oklch(93% 0.01 95);--text-muted:oklch(67% 0.02 95);"
    "--accent:oklch(76% 0.15 155);--accent-strong:oklch(84% 0.14 155);color-scheme:dark}"
    "body{background:var(--bg);color:var(--text)}a{color:var(--accent-strong)}</style>"
)
_THEME_SCRIPT = '<script src="/static/theme.js"></script>'

# Code blocks are highlighted server-side (Pygments) rather than via a
# vendored client-side highlighter — it needs zero CSP loosening (static
# colored spans, no script), unlike mermaid/math which both execute
# same-origin JS. Paired styles, "friendly"/"monokai", are Pygments'
# built-in light/dark counterparts (same pairing convention many doc
# themes use) — both style-defs rulesets are always emitted, scoped so the
# active one follows data-theme purely via CSS, no re-highlighting needed
# (the token->class mapping is fixed regardless of which formatter's style
# generates the markup, so either formatter works for the highlight() call
# itself).
_pygments_formatter = HtmlFormatter(nowrap=True, style="monokai")
_PYGMENTS_LIGHT_STYLE = HtmlFormatter(nowrap=True, style="friendly")
_PYGMENTS_DARK_STYLE = _pygments_formatter
_PYGMENTS_CLASSES = set(STANDARD_TYPES.values())
_PYGMENTS_CSS = (
    "<style>.codehilite{background:var(--surface-2);color:var(--text);padding:1rem;"
    "border-radius:6px;overflow-x:auto}"
    + _PYGMENTS_LIGHT_STYLE.get_style_defs(".codehilite")
    + _PYGMENTS_DARK_STYLE.get_style_defs('html[data-theme="dark"] .codehilite')
    + "</style>"
)


def render_highlighted_source(content: str, language: str | None) -> tuple[str, str]:
    """Read-only syntax-highlighted rendering of raw source text — shared by
    the code-format live view above and the dashboard's historical version
    viewer (versions.py/dashboard.py), since browsing an old snapshot should
    never re-execute it regardless of the artifact's actual format. No nh3
    needed: Pygments/html.escape already HTML-escape all source text, and
    this only ever builds a fixed <pre>/<code> skeleton, never arbitrary
    parsed markup. Returns (body_html, style_block)."""
    lexer = None
    if language:
        try:
            lexer = get_lexer_by_name(language)
        except ClassNotFound:
            lexer = None

    if lexer is not None:
        highlighted = highlight(content, lexer, _pygments_formatter)
        return f'<pre class="codehilite"><code>{highlighted}</code></pre>', _PYGMENTS_CSS
    return f"<pre>{html_escape.escape(content)}</pre>", ""


def _page_csp(has_math: bool) -> str:
    # Scripts/fonts/external styles stay off entirely beyond what's actually
    # used — each directive is only loosened for the specific same-origin
    # vendored asset that needs it, never an external CDN. script-src 'self'
    # is unconditional now: theme.js (light/dark tokens) loads on every one
    # of these pages, not just the ones with mermaid/math/csv-resize.
    style_src = "'unsafe-inline'" + (" 'self'" if has_math else "")  # 'self' for the vendored katex.min.css link
    parts = [
        "default-src 'none'",
        f"style-src {style_src}",
        "img-src data: blob:",
        "frame-ancestors 'self'",
        "script-src 'self'",
    ]
    if has_math:
        parts.append("font-src 'self'")  # katex.min.css loads its own woff2 fonts
    return "; ".join(parts)


_PAGE_CSP = _page_csp(has_math=False)

_CSV_ASSETS = '<script src="/static/csv-table-init.js"></script>'
_CSV_CSS = (
    "<style>body{font-family:var(--font-mono, monospace);margin:0;padding:1rem}"
    ".csv-table{border-collapse:collapse;font-family:var(--font-mono, monospace);font-size:0.9rem}"
    ".csv-table th,.csv-table td{border:1px solid var(--border);padding:4px 8px;text-align:left;"
    "overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".csv-table th{background:var(--surface-2);position:relative}"
    ".csv-col-resize-handle{position:absolute;top:0;right:0;width:6px;height:100%;"
    "cursor:col-resize;user-select:none}"
    ".csv-col-resize-handle:hover{background:var(--accent)}</style>"
)


def _render_csv_table(content: str) -> str:
    """Turn CSV content into a plain HTML table — no nh3 needed, every cell
    goes through html.escape and this only ever builds a fixed table
    skeleton, never arbitrary parsed markup (same trust model as
    render_highlighted_source above). First row is treated as a header,
    matching Claude's own CSV artifact preview and how agent-generated CSVs
    are typically shaped. Column-resize handles are added client-side by
    csv-table-init.js, not here."""
    rows = list(csv.reader(io.StringIO(content)))
    if not rows:
        return "<p>(empty)</p>"

    def _row(cells: list[str], tag: str) -> str:
        cells_html = "".join(f"<{tag}>{html_escape.escape(c)}</{tag}>" for c in cells)
        return f"<tr>{cells_html}</tr>"

    header, *body = rows
    thead = f"<thead>{_row(header, 'th')}</thead>"
    tbody = "<tbody>" + "".join(_row(r, "td") for r in body) + "</tbody>"
    return f'<table class="csv-table">{thead}{tbody}</table>'


def _fence_with_extras(
    self: RendererHTML, tokens: Sequence[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    token = tokens[idx]
    info = token.info.strip() if token.info else ""
    lang = info.split(maxsplit=1)[0] if info else ""

    if lang.lower() == "mermaid":
        env["has_mermaid"] = True
        return f'<pre class="mermaid">{html_escape.escape(token.content)}</pre>\n'

    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = None
        if lexer is not None:
            env["has_code_block"] = True
            highlighted = highlight(token.content, lexer, _pygments_formatter)
            return f'<pre class="codehilite"><code>{highlighted}</code></pre>\n'

    return RendererHTML.fence(self, tokens, idx, options, env)


_markdown = MarkdownIt("commonmark")
_markdown.add_render_rule("fence", _fence_with_extras)

# dollarmath_plugin registers its inline rule *before* the built-in "escape"
# rule, so backslash sequences inside $...$/$$...$$ (e.g. LaTeX's \\ line
# break in a matrix) are captured as raw math content before CommonMark's
# generic backslash-escape handling ever sees them — that escape rule is
# what silently collapsed \\ to \ when math was previously left as plain
# passthrough text with no dedicated parsing at all.
# allow_digits=False avoids treating ordinary prose like "$5 and $10" as
# math (a bare adjacent digit is almost never intentional math notation);
# allow_space=False likewise rejects "$ padded $" delimiters, which no
# real LaTeX author writes; allow_labels=False skips the equation-numbering
# extension (`$$...$$ (eq1)`) since it's not something we support anyway.
dollarmath_plugin(_markdown, allow_labels=False, allow_digits=False, allow_space=False)

# The plugin's default render rules already do exactly what we want
# (HTML-escape the raw math source, wrap in a class so the client-side
# KaTeX init script can find it) — wrap them only to additionally flag
# env["has_math"], mirroring has_mermaid/has_code_block above. These were
# already bound to the renderer instance by the plugin's own
# add_render_rule call (which does function.__get__(renderer)), so they're
# called with just (tokens, idx, options, env), not (self, ...).
_render_math_inline = _markdown.renderer.rules["math_inline"]
_render_math_block = _markdown.renderer.rules["math_block"]


def _math_inline_with_flag(self, tokens, idx, options, env):
    env["has_math"] = True
    return _render_math_inline(tokens, idx, options, env)


def _math_block_with_flag(self, tokens, idx, options, env):
    env["has_math"] = True
    return _render_math_block(tokens, idx, options, env)


_markdown.add_render_rule("math_inline", _math_inline_with_flag)
_markdown.add_render_rule("math_block", _math_block_with_flag)

_MERMAID_CLASS_RE = re.compile(r'class\s*=\s*["\'][^"\']*\bmermaid\b[^"\']*["\']', re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def _inject_mermaid_if_present(content: str) -> tuple[str, bool]:
    """HTML artifacts are otherwise served byte-for-byte — this is the one
    deliberate exception. An artifact that includes an element with a
    'mermaid' class (e.g. <pre class="mermaid">, the same convention Claude's
    own artifact viewer recognizes) gets the vendored mermaid runtime spliced
    in automatically, so publishers don't have to inline the whole library
    themselves just to draw a diagram."""
    if not _MERMAID_CLASS_RE.search(content):
        return content, False

    match = _BODY_CLOSE_RE.search(content)
    if match:
        return content[: match.start()] + _MERMAID_ASSETS + content[match.start() :], True
    return content + _MERMAID_ASSETS, True


async def _load_artifact(artifact_id: str) -> Artifact:
    async with session_scope() as session:
        result = await session.execute(select(Artifact).where(Artifact.id == artifact_id))
        artifact = result.scalar_one_or_none()
    if artifact is None:
        raise HTTPException(status_code=404)
    return artifact


@router.get("/a/{artifact_id}")
async def view_artifact(artifact_id: str) -> Response:
    artifact = await _load_artifact(artifact_id)

    if artifact.format == ArtifactFormat.markdown:
        env: dict = {}
        rendered = nh3.clean(
            _markdown.render(artifact.content, env),
            tag_attribute_values={
                "pre": {"class": {"mermaid", "codehilite"}},
                "span": {"class": _PYGMENTS_CLASSES | {"math inline"}},
                "div": {"class": {"math block"}},
            },
        )
        has_mermaid = env.get("has_mermaid", False)
        has_math = env.get("has_math", False)

        title = html_escape.escape(artifact.title or "Untitled")
        style = _PYGMENTS_CSS if env.get("has_code_block") else ""
        # theme.js first — mermaid-init.js reads documentElement's data-theme
        # attribute at mermaid.initialize() time, so it must already be set.
        assets = _THEME_SCRIPT + (_MERMAID_ASSETS if has_mermaid else "") + (_KATEX_ASSETS if has_math else "")
        csp = _page_csp(has_math)
        body = f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{style}{assets}<body>{rendered}</body>"
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": csp})

    if artifact.format == ArtifactFormat.code:
        # Read-only, syntax-highlighted, never executed — distinct from
        # the HTML format below, which iframes a sandboxed live preview.
        code_html, style = render_highlighted_source(artifact.content, artifact.language)

        title = html_escape.escape(artifact.title or "Untitled")
        safe_id = html_escape.escape(artifact_id)
        body = (
            f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{style}{_THEME_SCRIPT}"
            f'<body><p><a href="/a/{safe_id}/raw">View raw</a></p>{code_html}</body>'
        )
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": _PAGE_CSP})

    if artifact.format == ArtifactFormat.csv:
        # Read-only table view, never executed.
        table_html = _render_csv_table(artifact.content)

        title = html_escape.escape(artifact.title or "Untitled")
        safe_id = html_escape.escape(artifact_id)
        body = (
            f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{_CSV_CSS}{_THEME_SCRIPT}"
            f'<body><p><a href="/a/{safe_id}/raw">View raw</a></p>{table_html}{_CSV_ASSETS}</body>'
        )
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": _PAGE_CSP})

    title = html_escape.escape(artifact.title or "Untitled")
    safe_id = html_escape.escape(artifact_id)
    page = (
        f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{_THEME_SCRIPT}"
        "<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%}</style>"
        f'<iframe sandbox="allow-scripts" src="/a/{safe_id}/raw"></iframe>'
    )
    # frame-src 'self' is only needed here, to embed the same-origin /raw
    # iframe above — the markdown/code branches above never embed an
    # iframe, so _PAGE_CSP itself stays as tight as possible for them.
    csp = _PAGE_CSP + "; frame-src 'self'"
    return Response(content=page, media_type="text/html", headers={"Content-Security-Policy": csp})


@router.get("/a/{artifact_id}/raw")
async def view_artifact_raw(artifact_id: str) -> Response:
    artifact = await _load_artifact(artifact_id)

    if artifact.format == ArtifactFormat.code:
        # Exact source, no highlighting — text/plain never executes, so
        # no CSP header is needed here. nosniff is still worth setting: it's
        # defense-in-depth against a browser ever choosing to sniff a
        # declared text/plain response into something executable.
        return Response(
            content=artifact.content,
            media_type="text/plain",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    if artifact.format == ArtifactFormat.csv:
        # Exact source, no table rendering — text/csv never executes, so no
        # CSP header is needed here, same reasoning as the code branch above.
        return Response(
            content=artifact.content,
            media_type="text/csv",
            headers={"X-Content-Type-Options": "nosniff"},
        )

    if artifact.format != ArtifactFormat.html:
        raise HTTPException(status_code=404)

    content, has_mermaid = _inject_mermaid_if_present(artifact.content)
    csp = _HTML_CSP_WITH_MERMAID if has_mermaid else _HTML_CSP
    return Response(content=content, media_type="text/html", headers={"Content-Security-Policy": csp})
