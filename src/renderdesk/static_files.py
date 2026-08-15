from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles


class CORSStaticFiles(StaticFiles):
    """Serves /static with `Access-Control-Allow-Origin: *`.

    This exists for exactly one case, but it's a case the whole `html`/
    `react` artifact surface depends on. Those artifacts render inside an
    iframe carrying `sandbox allow-scripts` with no `allow-same-origin`
    (see view.py's _HTML_CSP), which is what stops artifact JS from
    reaching this app's real origin — deliberate, and not negotiable.

    A side effect of that sandbox is an opaque origin, and webfonts are the
    one subresource type always fetched in CORS mode: a `<script src>` or a
    `<link rel=stylesheet>` loads same-origin without any CORS involvement,
    but an `@font-face` `src` does not. From an opaque origin the browser
    sends `Origin: null`, so the vendored Bootstrap Icons woff2 counts as
    cross-origin and is blocked unless the response opts in. The visible
    symptom is a `bi-*` icon rendering as its raw Private Use Area
    codepoint (a tofu box reading e.g. F589) — the stylesheet applies,
    since it isn't CORS-gated, so `content: "\\f589"` lands with no glyph
    behind it.

    Applied to the whole mount rather than sniffing for font extensions:
    everything under /static is public, unauthenticated, non-credentialed
    vendored asset, so `*` grants nothing a plain server-side fetch of the
    same URL wouldn't. Per-extension logic would just be a trap the next
    time a font arrives. `*` without `Access-Control-Allow-Credentials`
    also means no cookies ever ride along with these requests.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response
