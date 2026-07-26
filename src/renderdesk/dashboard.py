from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from renderdesk import comments, shares
from renderdesk.config import settings
from renderdesk.db import session_scope
from renderdesk.models import Artifact, Connection, User
from renderdesk.session_auth import (
    LOGIN_PATH,
    SESSION_COOKIE_NAME,
    create_session,
    delete_session,
    require_current_user,
    verify_password,
)
from renderdesk.tools import NotFoundError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Only mark the cookie Secure when we're actually serving https — otherwise a
# browser (correctly) refuses to send it back, breaking login over plain
# http in local dev.
_COOKIE_SECURE = urlparse(settings.public_base_url).scheme == "https"


@router.get(LOGIN_PATH)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post(LOGIN_PATH)
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None or not verify_password(user, password):
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid email or password"}, status_code=401
            )
        token = await create_session(session, user)

    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.session_expiry_days * 86400,
    )
    return response


@router.post("/dashboard/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        async with session_scope() as session:
            await delete_session(session, token)
    response = RedirectResponse(url=LOGIN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/dashboard")
async def dashboard_home(request: Request, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Artifact, Connection.label)
                .join(Connection, Artifact.connection_id == Connection.id)
                .where(Connection.user_id == user.id)
                .order_by(Artifact.updated_at.desc())
            )
        ).all()
    artifacts = [{"artifact": artifact, "connection_label": label} for artifact, label in rows]

    async with session_scope() as session:
        shared_with_you = await shares.list_shared_with_user(session, user.id)

    return templates.TemplateResponse(
        request,
        "dashboard_list.html",
        {"artifacts": artifacts, "shared_with_you": shared_with_you, "user": user},
    )


@router.get("/dashboard/a/{artifact_id}")
async def dashboard_artifact(request: Request, artifact_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            artifact = await comments.get_accessible_artifact(session, user.id, artifact_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        threads = await comments.list_comments_for_dashboard(session, user, artifact_id)
    return templates.TemplateResponse(
        request, "dashboard_artifact.html", {"artifact": artifact, "threads": threads, "user": user}
    )


@router.post("/dashboard/a/{artifact_id}/comments")
async def dashboard_new_comment(artifact_id: str, body: str = Form(...), user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            await comments.create_comment(session, user, artifact_id, body)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/comments/{comment_id}/reply")
async def dashboard_reply(
    comment_id: str,
    artifact_id: str = Form(...),
    body: str = Form(...),
    user: User = Depends(require_current_user),
):
    async with session_scope() as session:
        try:
            await comments.reply_as_human(session, user, comment_id, body)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/comments/{comment_id}/resolve")
async def dashboard_resolve(
    comment_id: str, artifact_id: str = Form(...), user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            await comments.toggle_resolved(session, user, comment_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)
