from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from mcp.server.auth.provider import construct_redirect_uri
from sqlalchemy import delete, func, select

from renderdesk import comments, shares, tools, versions, view
from renderdesk.auth import generate_token, hash_token
from renderdesk.config import settings
from renderdesk.csrf import verify_csrf
from renderdesk.db import session_scope
from renderdesk.models import Artifact, ArtifactShare, Comment, Connection, OAuthClient, User, utcnow
from renderdesk.models import OAuthAccessToken as OAuthAccessTokenRow
from renderdesk.models import OAuthAuthorizationCode as OAuthAuthorizationCodeRow
from renderdesk.models import OAuthRefreshToken as OAuthRefreshTokenRow
from renderdesk.oauth_provider import oauth_provider
from renderdesk.quotas import QuotaExceededError
from renderdesk.session_auth import (
    LOGIN_PATH,
    SESSION_COOKIE_NAME,
    clear_failed_logins,
    create_session,
    delete_session,
    is_login_rate_limited,
    record_failed_login,
    require_current_user,
    safe_next_path,
    verify_password,
)
from renderdesk.tokens import create_personal_token
from renderdesk.tools import NotFoundError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Lets every template embed the current request's CSRF token via
# {{ csrf_token(request) }} without every TemplateResponse call needing to
# pass it through its context dict explicitly — `request` is already
# auto-injected into the Jinja context by TemplateResponse(request, ...).
templates.env.globals["csrf_token"] = lambda request: request.state.csrf_token

# Only mark the cookie Secure when we're actually serving https — otherwise a
# browser (correctly) refuses to send it back, breaking login over plain
# http in local dev.
_COOKIE_SECURE = urlparse(settings.public_base_url).scheme == "https"


@router.get(LOGIN_PATH)
async def login_form(request: Request, next: str = "/dashboard"):
    return templates.TemplateResponse(request, "login.html", {"error": None, "next": next})


@router.post(LOGIN_PATH, dependencies=[Depends(verify_csrf)])
async def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/dashboard")
):
    if is_login_rate_limited(email):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many failed attempts. Try again in a few minutes.", "next": next},
            status_code=429,
        )

    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None or not verify_password(user, password):
            record_failed_login(email)
            return templates.TemplateResponse(
                request, "login.html", {"error": "Invalid email or password", "next": next}, status_code=401
            )
        clear_failed_logins(email)
        token = await create_session(session, user)

    response = RedirectResponse(url=safe_next_path(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        max_age=settings.session_expiry_days * 86400,
    )
    return response


@router.post("/dashboard/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        async with session_scope() as session:
            await delete_session(session, token)
    response = RedirectResponse(url=LOGIN_PATH, status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/oauth/consent")
async def oauth_consent_form(request: Request, request_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        row = (
            await session.execute(select(OAuthAuthorizationCodeRow).where(OAuthAuthorizationCodeRow.id == request_id))
        ).scalar_one_or_none()
        if row is None or row.approved or row.expires_at <= utcnow():
            raise HTTPException(status_code=404)
        client_row = (
            await session.execute(select(OAuthClient).where(OAuthClient.client_id == row.client_id))
        ).scalar_one_or_none()
    client_name = (client_row.metadata_json.get("client_name") if client_row else None) or row.client_id
    return templates.TemplateResponse(
        request,
        "oauth_consent.html",
        {"request_id": request_id, "client_name": client_name, "scope": row.scope, "user": user},
    )


@router.post("/oauth/consent", dependencies=[Depends(verify_csrf)])
async def oauth_consent_submit(
    request_id: str = Form(...), action: str = Form(...), user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        row = (
            await session.execute(select(OAuthAuthorizationCodeRow).where(OAuthAuthorizationCodeRow.id == request_id))
        ).scalar_one_or_none()
        if row is None or row.approved or row.expires_at <= utcnow():
            raise HTTPException(status_code=404)

        redirect_uri = row.redirect_uri
        state = row.state

        if action != "approve":
            await session.delete(row)
            await session.commit()
            return RedirectResponse(
                construct_redirect_uri(redirect_uri, error="access_denied", state=state), status_code=303
            )

        code = generate_token()
        row.code_hash = hash_token(code)
        row.user_id = user.id
        row.approved = True
        await session.commit()

    return RedirectResponse(construct_redirect_uri(redirect_uri, code=code, state=state), status_code=303)


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


async def _artifact_context(user: User, artifact_id: str) -> dict:
    async with session_scope() as session:
        try:
            artifact = await comments.get_accessible_artifact(session, user.id, artifact_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        threads = await comments.list_comments_for_dashboard(session, user, artifact_id)
        owner_connection = (
            await session.execute(
                select(Connection.id).where(
                    Connection.id == artifact.connection_id, Connection.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        is_owner = owner_connection is not None
        share_list = await shares.list_shares(session, user.id, artifact_id) if is_owner else []
        other_connections = (
            (
                await session.execute(
                    select(Connection)
                    .where(
                        Connection.user_id == user.id,
                        Connection.revoked_at.is_(None),
                        Connection.id != artifact.connection_id,
                    )
                    .order_by(Connection.created_at.desc())
                )
            )
            .scalars()
            .all()
            if is_owner
            else []
        )
    return {
        "artifact": artifact,
        "threads": threads,
        "user": user,
        "is_owner": is_owner,
        "shares": share_list,
        "other_connections": other_connections,
    }


@router.get("/dashboard/a/{artifact_id}")
async def dashboard_artifact(request: Request, artifact_id: str, user: User = Depends(require_current_user)):
    context = await _artifact_context(user, artifact_id)
    return templates.TemplateResponse(
        request, "dashboard_artifact.html", {**context, "share_error": None, "reassign_error": None}
    )


@router.get("/dashboard/a/{artifact_id}/versions")
async def dashboard_artifact_versions(
    request: Request, artifact_id: str, user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            artifact = await tools.get_owned_artifact_by_user(session, user.id, artifact_id)
            history = await versions.list_versions(session, user.id, artifact_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    old_bytes = sum(v["byte_size"] for v in history if not v["is_current"])
    return templates.TemplateResponse(
        request,
        "dashboard_versions.html",
        {"artifact": artifact, "versions": history, "old_bytes": old_bytes, "user": user, "prune_error": None},
    )


@router.get("/dashboard/a/{artifact_id}/versions/{version}")
async def dashboard_view_version(
    request: Request, artifact_id: str, version: int, user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            artifact = await tools.get_owned_artifact_by_user(session, user.id, artifact_id)
            version_row = await versions.get_version(session, user.id, artifact_id, version)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    # Always rendered as read-only highlighted source, regardless of the
    # artifact's actual format — browsing history is for looking at what a
    # past snapshot contained, never for re-executing it (an old HTML
    # snapshot doesn't get a live sandboxed preview here, unlike the
    # current version at /a/{id}).
    body_html, style = view.render_highlighted_source(version_row.content, version_row.language)
    return templates.TemplateResponse(
        request,
        "dashboard_version_detail.html",
        {
            "artifact": artifact,
            "version": version_row,
            "body_html": body_html,
            "style": style,
            "user": user,
        },
    )


@router.post("/dashboard/a/{artifact_id}/versions/{version}/delete", dependencies=[Depends(verify_csrf)])
async def dashboard_delete_version(artifact_id: str, version: int, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            await versions.delete_version(session, user.id, artifact_id, version)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        except versions.CannotDeleteCurrentVersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}/versions", status_code=303)


@router.post("/dashboard/a/{artifact_id}/versions/prune", dependencies=[Depends(verify_csrf)])
async def dashboard_prune_versions(request: Request, artifact_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            await versions.prune_old_versions(session, user.id, artifact_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}/versions", status_code=303)


@router.post("/dashboard/a/{artifact_id}/share", dependencies=[Depends(verify_csrf)])
async def dashboard_share_artifact(
    request: Request, artifact_id: str, email: str = Form(...), user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            await shares.share_artifact_from_dashboard(session, user.id, artifact_id, email)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        except (shares.RecipientNotFoundError, shares.SelfShareError) as exc:
            context = await _artifact_context(user, artifact_id)
            return templates.TemplateResponse(
                request,
                "dashboard_artifact.html",
                {**context, "share_error": str(exc), "reassign_error": None},
                status_code=400,
            )
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/a/{artifact_id}/shares/{share_id}/unshare", dependencies=[Depends(verify_csrf)])
async def dashboard_unshare_artifact(
    artifact_id: str, share_id: str, user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            await shares.unshare_artifact(session, user.id, artifact_id, share_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/a/{artifact_id}/reassign", dependencies=[Depends(verify_csrf)])
async def dashboard_reassign_artifact(
    request: Request,
    artifact_id: str,
    target_connection_id: str = Form(...),
    user: User = Depends(require_current_user),
):
    async with session_scope() as session:
        try:
            await tools.reassign_artifact_connection(session, user.id, artifact_id, target_connection_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        except (tools.InvalidTargetConnectionError, QuotaExceededError) as exc:
            context = await _artifact_context(user, artifact_id)
            return templates.TemplateResponse(
                request,
                "dashboard_artifact.html",
                {**context, "share_error": None, "reassign_error": str(exc)},
                status_code=400,
            )
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/a/{artifact_id}/delete", dependencies=[Depends(verify_csrf)])
async def dashboard_delete_artifact(artifact_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            await tools.delete_artifact(session, user.id, artifact_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/dashboard/a/{artifact_id}/comments", dependencies=[Depends(verify_csrf)])
async def dashboard_new_comment(artifact_id: str, body: str = Form(...), user: User = Depends(require_current_user)):
    async with session_scope() as session:
        try:
            await comments.create_comment(session, user, artifact_id, body)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
        except comments.CommentTooLargeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/comments/{comment_id}/reply", dependencies=[Depends(verify_csrf)])
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
        except comments.CommentTooLargeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


@router.post("/dashboard/comments/{comment_id}/resolve", dependencies=[Depends(verify_csrf)])
async def dashboard_resolve(
    comment_id: str, artifact_id: str = Form(...), user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        try:
            await comments.toggle_resolved(session, user, comment_id)
        except NotFoundError:
            raise HTTPException(status_code=404) from None
    return RedirectResponse(url=f"/dashboard/a/{artifact_id}", status_code=303)


async def _connections_context(user: User) -> dict:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Connection).where(Connection.user_id == user.id).order_by(Connection.created_at.desc())
            )
        ).scalars().all()
        client_ids = {c.client_id for c in rows if c.client_id}
        client_names: dict[str, str] = {}
        if client_ids:
            client_rows = (
                await session.execute(select(OAuthClient).where(OAuthClient.client_id.in_(client_ids)))
            ).scalars().all()
            client_names = {c.client_id: (c.metadata_json.get("client_name") or c.client_id) for c in client_rows}

    connections = [
        {
            "connection": c,
            "kind": "OAuth" if c.client_id else "Personal token",
            "client_name": client_names.get(c.client_id) if c.client_id else None,
        }
        for c in rows
    ]
    return {"connections": connections, "user": user}


@router.get("/dashboard/connections")
async def dashboard_connections(request: Request, user: User = Depends(require_current_user)):
    context = await _connections_context(user)
    return templates.TemplateResponse(request, "dashboard_connections.html", {**context, "error": None})


@router.post("/dashboard/connections/tokens", dependencies=[Depends(verify_csrf)])
async def dashboard_create_token(
    request: Request, label: str = Form(None), user: User = Depends(require_current_user)
):
    async with session_scope() as session:
        token = await create_personal_token(session, user.id, label or None)
    return templates.TemplateResponse(request, "dashboard_token_created.html", {"token": token, "user": user})


@router.post("/dashboard/connections/{connection_id}/revoke", dependencies=[Depends(verify_csrf)])
async def dashboard_revoke_connection(connection_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        connection = (
            await session.execute(
                select(Connection).where(Connection.id == connection_id, Connection.user_id == user.id)
            )
        ).scalar_one_or_none()
        if connection is None:
            raise HTTPException(status_code=404)

    # Shared helper: harmlessly no-ops the OAuth-token cleanup for a
    # personal-token connection, since it has none.
    await oauth_provider.revoke_connection(connection_id)
    return RedirectResponse(url="/dashboard/connections", status_code=303)


@router.post("/dashboard/connections/{connection_id}/delete", dependencies=[Depends(verify_csrf)])
async def dashboard_delete_connection(request: Request, connection_id: str, user: User = Depends(require_current_user)):
    async with session_scope() as session:
        connection = (
            await session.execute(
                select(Connection).where(Connection.id == connection_id, Connection.user_id == user.id)
            )
        ).scalar_one_or_none()
        if connection is None:
            raise HTTPException(status_code=404)
        if connection.revoked_at is None:
            raise HTTPException(status_code=400, detail="Only revoked connections can be deleted")

        # A connection is never deleted out from under content it published —
        # if the owner wants that content gone too, deleting the artifacts
        # themselves (see dashboard_delete_artifact) is the explicit way to
        # do it, rather than an implicit side effect of cleaning up a token.
        artifact_count = (
            await session.execute(
                select(func.count()).select_from(Artifact).where(Artifact.connection_id == connection_id)
            )
        ).scalar_one()
        comment_count = (
            await session.execute(
                select(func.count()).select_from(Comment).where(Comment.author_connection_id == connection_id)
            )
        ).scalar_one()
        share_count = (
            await session.execute(
                select(func.count())
                .select_from(ArtifactShare)
                .where(ArtifactShare.shared_by_connection_id == connection_id)
            )
        ).scalar_one()

        if artifact_count or comment_count or share_count:
            context = await _connections_context(user)
            error = (
                f"Can't delete {connection.label or 'this connection'} — it published "
                f"{artifact_count} artifact(s), {comment_count} comment(s), and "
                f"{share_count} share(s). Revoked connections keep their history."
            )
            return templates.TemplateResponse(
                request, "dashboard_connections.html", {**context, "error": error}, status_code=400
            )

        # Bookkeeping rows only (credentials, not content) — safe to clean up.
        await session.execute(delete(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.connection_id == connection_id))
        await session.execute(delete(OAuthAccessTokenRow).where(OAuthAccessTokenRow.connection_id == connection_id))
        await session.delete(connection)
        await session.commit()

    return RedirectResponse(url="/dashboard/connections", status_code=303)
