import html as html_escape
import re
from typing import Sequence

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

# Code blocks are highlighted server-side (Pygments) rather than via a
# vendored client-side highlighter — it needs zero CSP loosening (static
# colored spans, no script), unlike mermaid/math which both execute
# same-origin JS. "monokai" matches the dark theme used elsewhere
# (mermaid's theme: "dark", the dashboard's dark background).
_pygments_formatter = HtmlFormatter(nowrap=True, style="monokai")
_PYGMENTS_CLASSES = set(STANDARD_TYPES.values())
_PYGMENTS_CSS = (
    "<style>.codehilite{background:#1e1e1e;color:#f8f8f2;padding:1rem;"
    "border-radius:6px;overflow-x:auto}" + _pygments_formatter.get_style_defs() + "</style>"
)


def _page_csp(has_mermaid: bool, has_math: bool) -> str:
    # Scripts/fonts/external styles stay off entirely for ordinary markdown
    # (it's sanitized, not executable, by design) — each directive is only
    # loosened for the specific same-origin vendored asset that needs it,
    # never an external CDN.
    style_src = "'unsafe-inline'" + (" 'self'" if has_math else "")  # 'self' for the vendored katex.min.css link
    parts = ["default-src 'none'", "frame-src 'self'", f"style-src {style_src}", "img-src data: blob:"]
    if has_mermaid or has_math:
        parts.append("script-src 'self'")
    if has_math:
        parts.append("font-src 'self'")  # katex.min.css loads its own woff2 fonts
    return "; ".join(parts)


_PAGE_CSP = _page_csp(has_mermaid=False, has_math=False)


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
        assets = (_MERMAID_ASSETS if has_mermaid else "") + (_KATEX_ASSETS if has_math else "")
        csp = _page_csp(has_mermaid, has_math)
        body = f"<!doctype html><title>{title}</title>{style}{assets}<body>{rendered}</body>"
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": csp})

    if artifact.format == ArtifactFormat.code:
        # Read-only, syntax-highlighted, never executed — distinct from
        # the HTML format below, which iframes a sandboxed live preview.
        # Reuses the exact same Pygments setup as markdown fenced code
        # blocks; no nh3 needed since Pygments already HTML-escapes
        # source text and this builds a fixed skeleton, not arbitrary
        # parsed markup.
        lexer = None
        if artifact.language:
            try:
                lexer = get_lexer_by_name(artifact.language)
            except ClassNotFound:
                lexer = None

        if lexer is not None:
            highlighted = highlight(artifact.content, lexer, _pygments_formatter)
            code_html = f'<pre class="codehilite"><code>{highlighted}</code></pre>'
            style = _PYGMENTS_CSS
        else:
            code_html = f"<pre>{html_escape.escape(artifact.content)}</pre>"
            style = ""

        title = html_escape.escape(artifact.title or "Untitled")
        body = (
            f"<!doctype html><title>{title}</title>{style}"
            f'<body><p><a href="/a/{artifact_id}/raw">View raw</a></p>{code_html}</body>'
        )
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": _PAGE_CSP})

    title = html_escape.escape(artifact.title or "Untitled")
    page = (
        f"<!doctype html><title>{title}</title>"
        "<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%}</style>"
        f'<iframe sandbox="allow-scripts" src="/a/{artifact_id}/raw"></iframe>'
    )
    return Response(content=page, media_type="text/html", headers={"Content-Security-Policy": _PAGE_CSP})


@router.get("/a/{artifact_id}/raw")
async def view_artifact_raw(artifact_id: str) -> Response:
    artifact = await _load_artifact(artifact_id)

    if artifact.format == ArtifactFormat.code:
        # Exact source, no highlighting — text/plain never executes, so
        # no CSP header is needed here.
        return Response(content=artifact.content, media_type="text/plain")

    if artifact.format != ArtifactFormat.html:
        raise HTTPException(status_code=404)

    content, has_mermaid = _inject_mermaid_if_present(artifact.content)
    csp = _HTML_CSP_WITH_MERMAID if has_mermaid else _HTML_CSP
    return Response(content=content, media_type="text/html", headers={"Content-Security-Policy": csp})
