import html as html_escape

import nh3
from fastapi import APIRouter, HTTPException, Response
from markdown_it import MarkdownIt
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

_PAGE_CSP = "default-src 'none'; frame-src 'self'; style-src 'unsafe-inline'; img-src data: blob:"

_markdown = MarkdownIt("commonmark")


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
        rendered = nh3.clean(_markdown.render(artifact.content))
        title = html_escape.escape(artifact.title or "Untitled")
        body = f"<!doctype html><title>{title}</title><body>{rendered}</body>"
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

    if artifact.format != ArtifactFormat.html:
        raise HTTPException(status_code=404)

    return Response(
        content=artifact.content, media_type="text/html", headers={"Content-Security-Policy": _HTML_CSP}
    )
