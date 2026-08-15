import csv
import html as html_escape
import io
import json
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
# 'self' (same-origin — our own vendored files, never an external CDN) is
# only added to the specific directive an artifact actually needs it for,
# and only when detected: script-src for a mermaid diagram or a rewritten
# cdnjs library reference (_inject_mermaid_if_present,
# _rewrite_cdn_library_urls), style-src/font-src for Bootstrap Icons'
# vendored CSS + webfont (_inject_bootstrap_icons_if_present). Kept as two
# independent axes rather than one flat "has extras" flag, since a
# same-origin script and a same-origin stylesheet+font are different CSP
# directives — a diagram-only artifact shouldn't get font-src widened, and
# an icons-only artifact shouldn't get script-src widened.
def _with_same_origin_scripts(csp: str) -> str:
    return csp.replace("script-src 'unsafe-inline' 'unsafe-eval';", "script-src 'unsafe-inline' 'unsafe-eval' 'self';")


def _with_same_origin_styles_and_fonts(csp: str) -> str:
    csp = csp.replace("style-src 'unsafe-inline';", "style-src 'unsafe-inline' 'self';")
    return csp.replace("font-src data:;", "font-src data: 'self';")

_MERMAID_VERSION = "11.16.0"
_MERMAID_ASSETS = (
    f'<script src="/static/vendor/mermaid-{_MERMAID_VERSION}.min.js"></script>'
    '<script src="/static/mermaid-init.js"></script>'
)

# Unlike _HTML_CSP, this needs no 'unsafe-inline' for script-src: every
# script tag react-init.js's wrapper page emits is an external /static file
# we control, never inline artifact-authored script (the JSX source itself
# sits inert in a type="application/json" block, only ever reaching Babel
# via JSON.parse). 'unsafe-eval' is still required — react-init.js runs
# Babel's transpiled output through `new Function(...)`, since there's no
# server-side bundling step to produce a plain <script> in the first place.
_REACT_CSP = _HTML_CSP.replace("script-src 'unsafe-inline' 'unsafe-eval';", "script-src 'self' 'unsafe-eval';")

_REACT_VERSION = "18.3.1"
_BABEL_STANDALONE_VERSION = "7.26.9"
_REACT_ASSETS = (
    f'<script src="/static/vendor/react-{_REACT_VERSION}.production.min.js"></script>'
    f'<script src="/static/vendor/react-dom-{_REACT_VERSION}.production.min.js"></script>'
    f'<script src="/static/vendor/babel-standalone-{_BABEL_STANDALONE_VERSION}.min.js"></script>'
    '<script src="/static/react-init.js"></script>'
)

# Optional libraries `react` artifacts may import beyond react/react-dom,
# and `html` artifacts may reference via the cdnjs rewrite below. Each has
# an official standalone/UMD browser build, vendored the same
# download-and-pin way as react/mermaid above — no bundler. Two exceptions
# to "download the current release": SheetJS moved off npm/jsDelivr after
# v0.18.6 (this comes from cdn.sheetjs.com instead), and Three.js dropped
# its classic global-exposing build after r160 — later releases only ship
# ES-module-only builds, so this is pinned at r160 deliberately, not
# because it's the latest. mathjs and Tone.js don't publish a minified
# browser build at all (only an unminified UMD bundle) — vendored as-is.
_THREE_VERSION = "0.160.0"
_LODASH_VERSION = "4.18.1"
_D3_VERSION = "7.9.0"
_MATHJS_VERSION = "15.2.0"
_CHARTJS_VERSION = "4.5.1"
_TONE_VERSION = "15.1.22"
_PAPAPARSE_VERSION = "5.6.0"
_XLSX_VERSION = "0.20.3"


def _specifier_pattern(*specifiers: str) -> re.Pattern[str]:
    alternation = "|".join(re.escape(s) for s in specifiers)
    return re.compile(rf"""(?:from\s+|import\s+|require\(\s*)['"](?:{alternation})['"]""")


# (import-specifier pattern, <script> tag to emit if that pattern matches
# an artifact's source). `chart.js` and `chart.js/auto` both resolve to
# the same vendored file: the UMD build already auto-registers every
# controller/element/plugin, so there's no separate "auto" asset to pick.
_REACT_OPTIONAL_LIBRARIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_specifier_pattern("three"), f'<script src="/static/vendor/three-{_THREE_VERSION}.min.js"></script>'),
    (_specifier_pattern("lodash"), f'<script src="/static/vendor/lodash-{_LODASH_VERSION}.min.js"></script>'),
    (_specifier_pattern("d3"), f'<script src="/static/vendor/d3-{_D3_VERSION}.min.js"></script>'),
    (_specifier_pattern("mathjs"), f'<script src="/static/vendor/mathjs-{_MATHJS_VERSION}.js"></script>'),
    (
        _specifier_pattern("chart.js", "chart.js/auto"),
        f'<script src="/static/vendor/chart.js-{_CHARTJS_VERSION}.min.js"></script>',
    ),
    (_specifier_pattern("tone"), f'<script src="/static/vendor/tone-{_TONE_VERSION}.js"></script>'),
    (_specifier_pattern("papaparse"), f'<script src="/static/vendor/papaparse-{_PAPAPARSE_VERSION}.min.js"></script>'),
    (_specifier_pattern("xlsx"), f'<script src="/static/vendor/xlsx-{_XLSX_VERSION}.full.min.js"></script>'),
)


def _optional_react_assets(source: str) -> str:
    """Scan a React artifact's JSX/TSX source for imports of the optional
    vendored libraries above and return only the <script> tags actually
    needed — loading all of them unconditionally on every render would be
    real, avoidable weight (Three.js and xlsx alone are hundreds of KB
    each). This is a regex heuristic over raw source, not a real JS parser
    (same rigor as _MERMAID_CLASS_RE below): it can miss a dynamically
    aliased import, but react-init.js's requireShim has its own
    undefined-check backstop for exactly that gap, so a miss here fails
    as a readable in-page error rather than a silent blank page."""
    return "".join(tag for pattern, tag in _REACT_OPTIONAL_LIBRARIES if pattern.search(source))


_BOOTSTRAP_ICONS_VERSION = "1.13.1"
_BOOTSTRAP_ICONS_ASSET = (
    f'<link rel="stylesheet" href="/static/vendor/bootstrap-icons/'
    f'bootstrap-icons-{_BOOTSTRAP_ICONS_VERSION}.min.css">'
)
# Bootstrap Icons isn't a JS import at all — just a CSS class
# (`bi bi-camera`) backed by a vendored webfont — so unlike the libraries
# above it needs no requireShim entry and applies identically to `html`
# and `react`. Matches on any `bi-<name>` token inside a class/className
# attribute; a plain-text false positive (e.g. a sentence containing
# "bi-weekly" outside any class attribute) can't match since the token has
# to sit inside quotes following class= or className=.
_BOOTSTRAP_ICONS_CLASS_RE = re.compile(
    r"""class(?:Name)?\s*=\s*["'][^"']*\bbi-[a-z0-9-]+\b[^"']*["']""", re.IGNORECASE
)


def _inject_bootstrap_icons_if_present(content: str) -> tuple[str, bool]:
    if not _BOOTSTRAP_ICONS_CLASS_RE.search(content):
        return content, False
    match = _BODY_CLOSE_RE.search(content)
    if match:
        return content[: match.start()] + _BOOTSTRAP_ICONS_ASSET + content[match.start() :], True
    return content + _BOOTSTRAP_ICONS_ASSET, True


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


_CDNJS_SCRIPT_RE = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*["']https://cdnjs\.cloudflare\.com/ajax/libs/"""
    r"""(?P<slug>[^/"']+)/[^/"']+/[^"']+["'][^>]*>\s*</script>""",
    re.IGNORECASE,
)

# cdnjs "library slug" (the path segment right after /ajax/libs/, distinct
# from the npm package name — e.g. lodash's slug is "lodash.js", not
# "lodash") to the same vendored asset _REACT_OPTIONAL_LIBRARIES emits.
# Confirmed against cdnjs's own listing at implementation time, not
# guessed — cdnjs slugs don't reliably match npm package names.
_CDNJS_SLUG_TO_ASSET = {
    "three.js": f'<script src="/static/vendor/three-{_THREE_VERSION}.min.js"></script>',
    "lodash.js": f'<script src="/static/vendor/lodash-{_LODASH_VERSION}.min.js"></script>',
    "d3": f'<script src="/static/vendor/d3-{_D3_VERSION}.min.js"></script>',
    "mathjs": f'<script src="/static/vendor/mathjs-{_MATHJS_VERSION}.js"></script>',
    "Chart.js": f'<script src="/static/vendor/chart.js-{_CHARTJS_VERSION}.min.js"></script>',
    "tone": f'<script src="/static/vendor/tone-{_TONE_VERSION}.js"></script>',
    "PapaParse": f'<script src="/static/vendor/papaparse-{_PAPAPARSE_VERSION}.min.js"></script>',
    "xlsx": f'<script src="/static/vendor/xlsx-{_XLSX_VERSION}.full.min.js"></script>',
}


def _rewrite_cdn_library_urls(content: str) -> tuple[str, bool]:
    """The other deliberate exception to "html artifacts never reach an
    external host": a <script src> pointing at the cdnjs.cloudflare.com
    allowlist Claude's own artifact sandbox uses (the URL pattern a model
    is already trained to reach for) gets rewritten in place to the
    same-origin vendored equivalent, rather than silently failing under
    this app's CSP. Any integrity/crossorigin attributes on the original
    tag are dropped along with it — they'd be meaningless, or actively
    wrong, once pointed at a different file. Slugs not in
    _CDNJS_SLUG_TO_ASSET are left untouched."""
    rewritten = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal rewritten
        asset = _CDNJS_SLUG_TO_ASSET.get(match.group("slug"))
        if asset is None:
            return match.group(0)
        rewritten = True
        return asset

    return _CDNJS_SCRIPT_RE.sub(_replace, content), rewritten


def _build_react_raw_html(artifact: Artifact) -> tuple[str, bool]:
    """Wrap a React artifact's JSX/TSX source (just the module body — no
    bundler, so no build step turns it into a plain <script>) for
    react-init.js to transpile and mount client-side. The source is embedded
    as JSON text, not an inline <script type="text/babel">, specifically so
    it never needs 'unsafe-inline' in the CSP: a type="application/json"
    block is inert data the HTML parser doesn't execute, reaching Babel only
    via JSON.parse. The HTML tokenizer still scans script *content* for a
    literal "</script" regardless of the declared type, though, so any such
    sequence in the source has to be escaped or it would truncate the tag
    early — the json.dumps().replace() below is the standard fix for
    embedding arbitrary text in an inline script (same technique frameworks
    use for SSR hydration data). Also emits <script> tags for any optional
    vendored library the source imports (_optional_react_assets) and a
    Bootstrap Icons <link> if a bi-* class is used — the returned bool
    tells the caller whether the response's CSP needs style-src/font-src
    widened for the latter (script-src is already 'self'-scoped for every
    react response, so the vendored libraries' <script> tags never need a
    CSP change of their own)."""
    title = html_escape.escape(artifact.title or "Untitled")
    source_json = json.dumps(artifact.content).replace("</", "<\\/")
    has_bootstrap_icons = bool(_BOOTSTRAP_ICONS_CLASS_RE.search(artifact.content))
    icons_asset = _BOOTSTRAP_ICONS_ASSET if has_bootstrap_icons else ""
    content = (
        f"<!doctype html><title>{title}</title><style>html,body{{margin:0}}</style>"
        f"{icons_asset}"
        '<div id="root"></div>'
        f'<script id="artifact-source" type="application/json">{source_json}</script>'
        f"{_REACT_ASSETS}"
        f"{_optional_react_assets(artifact.content)}"
    )
    return content, has_bootstrap_icons


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
        body = (
            f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{style}{_THEME_SCRIPT}"
            f"<body>{code_html}</body>"
        )
        return Response(content=body, media_type="text/html", headers={"Content-Security-Policy": _PAGE_CSP})

    if artifact.format == ArtifactFormat.csv:
        # Read-only table view, never executed.
        table_html = _render_csv_table(artifact.content)

        title = html_escape.escape(artifact.title or "Untitled")
        body = (
            f"<!doctype html><title>{title}</title>{_THEME_TOKENS_CSS}{_CSV_CSS}{_THEME_SCRIPT}"
            f"<body>{table_html}{_CSV_ASSETS}</body>"
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

    if artifact.format == ArtifactFormat.react:
        content, has_bootstrap_icons = _build_react_raw_html(artifact)
        csp = _with_same_origin_styles_and_fonts(_REACT_CSP) if has_bootstrap_icons else _REACT_CSP
        return Response(content=content, media_type="text/html", headers={"Content-Security-Policy": csp})

    if artifact.format != ArtifactFormat.html:
        raise HTTPException(status_code=404)

    content, has_mermaid = _inject_mermaid_if_present(artifact.content)
    content, has_cdn_rewrite = _rewrite_cdn_library_urls(content)
    content, has_bootstrap_icons = _inject_bootstrap_icons_if_present(content)
    csp = _HTML_CSP
    if has_mermaid or has_cdn_rewrite:
        csp = _with_same_origin_scripts(csp)
    if has_bootstrap_icons:
        csp = _with_same_origin_styles_and_fonts(csp)
    return Response(content=content, media_type="text/html", headers={"Content-Security-Policy": csp})
