import asyncio
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import delete, select, update

from renderdesk.auth import generate_token, hash_token
from renderdesk.db import session_scope
from renderdesk.models import Connection, OAuthClient, utcnow
from renderdesk.models import OAuthAccessToken as OAuthAccessTokenRow
from renderdesk.models import OAuthAuthorizationCode as OAuthAuthorizationCodeRow
from renderdesk.models import OAuthRefreshToken as OAuthRefreshTokenRow

ACCESS_TOKEN_TTL = timedelta(hours=1)
REFRESH_TOKEN_TTL = timedelta(days=180)
# A registered client that's never actually completed a token exchange
# (abandoned mid-flow, or registered and never used) after this long is
# swept by sweep_expired_oauth_rows — see app.py's lifespan.
UNUSED_CLIENT_TTL = timedelta(days=30)

# Guards _get_or_create_connection's SELECT-then-INSERT against two
# concurrent token exchanges for the same (user_id, client_id) both seeing
# no existing active connection and both creating one. Same process-local
# per-key locking approach as quotas.quota_lock, for the same reason
# (SQLite's own locking doesn't close a read-then-write race across two
# separate transactions).
_connection_creation_locks: dict[tuple[str | None, str], asyncio.Lock] = defaultdict(asyncio.Lock)
AUTHORIZATION_CODE_TTL = timedelta(minutes=10)


def _ts(dt: datetime) -> float:
    # Naive-but-UTC (see models.utcnow) — attach tzinfo explicitly rather
    # than letting .timestamp() assume the system's local timezone.
    return dt.replace(tzinfo=UTC).timestamp()


class RenderdeskOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with session_scope() as session:
            row = (
                await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            return OAuthClientInformationFull.model_validate(row.metadata_json)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with session_scope() as session:
            session.add(
                OAuthClient(
                    client_id=client_info.client_id,
                    metadata_json=client_info.model_dump(mode="json"),
                    created_at=utcnow(),
                )
            )
            await session.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        async with session_scope() as session:
            row = OAuthAuthorizationCodeRow(
                id=str(uuid.uuid4()),
                client_id=client.client_id,
                user_id=None,
                redirect_uri=str(params.redirect_uri),
                code_challenge=params.code_challenge,
                scope=" ".join(params.scopes) if params.scopes else None,
                state=params.state,
                code_hash=None,
                approved=False,
                expires_at=utcnow() + AUTHORIZATION_CODE_TTL,
            )
            session.add(row)
            await session.commit()
            request_id = row.id
        return f"/oauth/consent?request_id={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with session_scope() as session:
            code_hash = hash_token(authorization_code)
            row = (
                await session.execute(
                    select(OAuthAuthorizationCodeRow).where(
                        OAuthAuthorizationCodeRow.code_hash == code_hash,
                        OAuthAuthorizationCodeRow.client_id == client.client_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None or not row.approved or row.expires_at <= utcnow():
                return None
            return AuthorizationCode(
                code=authorization_code,
                scopes=row.scope.split(" ") if row.scope else [],
                expires_at=_ts(row.expires_at),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=row.redirect_uri,
                redirect_uri_provided_explicitly=True,
                subject=row.user_id,
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if authorization_code.subject is None:
            # Shouldn't happen: load_authorization_code only returns rows with
            # approved=True, which is only ever set alongside user_id in the
            # consent-approval route.
            raise TokenError(error="invalid_grant", error_description="authorization code has no associated user")

        code_hash = hash_token(authorization_code.code)
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(OAuthAuthorizationCodeRow).where(OAuthAuthorizationCodeRow.code_hash == code_hash)
                )
            ).scalar_one_or_none()
            if row is None:
                raise TokenError(error="invalid_grant", error_description="authorization code already used or expired")
            # Single-use: the SDK doesn't enforce this for us.
            await session.delete(row)
            await session.commit()

        connection_id = await self._get_or_create_connection(authorization_code.subject, client.client_id)
        return await self._issue_tokens(connection_id, client.client_id, authorization_code.scopes)

    async def _get_or_create_connection(self, user_id: str | None, client_id: str) -> str:
        async with _connection_creation_locks[(user_id, client_id)]:
            async with session_scope() as session:
                existing = (
                    await session.execute(
                        select(Connection).where(
                            Connection.user_id == user_id,
                            Connection.client_id == client_id,
                            Connection.revoked_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return existing.id

                oauth_client_row = (
                    await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
                ).scalar_one()
                client_name = oauth_client_row.metadata_json.get("client_name") or f"OAuth client {client_id[:8]}"

                connection = Connection(
                    id=str(uuid.uuid4()), user_id=user_id, client_id=client_id, label=client_name
                )
                session.add(connection)
                await session.commit()
                return connection.id

    async def _issue_tokens(self, connection_id: str, client_id: str, scopes: list[str]) -> OAuthToken:
        access_token = generate_token()
        refresh_token = generate_token()
        async with session_scope() as session:
            session.add(
                OAuthAccessTokenRow(
                    id=str(uuid.uuid4()),
                    token_hash=hash_token(access_token),
                    connection_id=connection_id,
                    expires_at=utcnow() + ACCESS_TOKEN_TTL,
                )
            )
            session.add(
                OAuthRefreshTokenRow(
                    id=str(uuid.uuid4()),
                    token_hash=hash_token(refresh_token),
                    connection_id=connection_id,
                    client_id=client_id,
                    expires_at=utcnow() + REFRESH_TOKEN_TTL,
                )
            )
            await session.commit()
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
            refresh_token=refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        async with session_scope() as session:
            token_hash = hash_token(refresh_token)
            row = (
                await session.execute(
                    select(OAuthRefreshTokenRow).where(
                        OAuthRefreshTokenRow.token_hash == token_hash,
                        OAuthRefreshTokenRow.client_id == client.client_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            connection = (
                await session.execute(select(Connection).where(Connection.id == row.connection_id))
            ).scalar_one()
            # Deliberately returned even if row.revoked_at is set — the reuse
            # check happens in exchange_refresh_token, not here. The SDK's own
            # expiry check (against expires_at) still applies normally.
            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=[],
                expires_at=int(_ts(row.expires_at)),
                subject=connection.user_id,
            )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        token_hash = hash_token(refresh_token.token)
        reused_connection_id = None
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.token_hash == token_hash)
                )
            ).scalar_one_or_none()
            if row is None:
                raise TokenError(error="invalid_grant", error_description="unknown refresh token")

            if row.revoked_at is not None:
                # This token was already rotated away — someone is replaying
                # a stale refresh token, the signal that it (or a later one
                # in the chain) was stolen. Kill the whole connection below,
                # outside this session.
                reused_connection_id = row.connection_id
            else:
                row.revoked_at = utcnow()
                connection_id = row.connection_id
                client_id = row.client_id
                await session.commit()

        if reused_connection_id is not None:
            await self.revoke_connection(reused_connection_id)
            raise TokenError(
                error="invalid_grant", error_description="refresh token reuse detected; connection revoked"
            )

        return await self._issue_tokens(connection_id, client_id, scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with session_scope() as session:
            token_hash = hash_token(token)
            result = (
                await session.execute(
                    select(OAuthAccessTokenRow, Connection)
                    .join(Connection, OAuthAccessTokenRow.connection_id == Connection.id)
                    .where(OAuthAccessTokenRow.token_hash == token_hash)
                )
            ).first()
            if result is None:
                return None
            access_row, connection = result
            if access_row.expires_at <= utcnow() or connection.revoked_at is not None:
                return None
            return AccessToken(
                token=token,
                client_id=connection.client_id or "",
                scopes=[],
                expires_at=int(_ts(access_row.expires_at)),
                subject=connection.id,
            )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        connection_id = None
        async with session_scope() as session:
            token_hash = hash_token(token.token)
            access_row = (
                await session.execute(select(OAuthAccessTokenRow).where(OAuthAccessTokenRow.token_hash == token_hash))
            ).scalar_one_or_none()
            if access_row is not None:
                connection_id = access_row.connection_id
            else:
                refresh_row = (
                    await session.execute(
                        select(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.token_hash == token_hash)
                    )
                ).scalar_one_or_none()
                if refresh_row is not None:
                    connection_id = refresh_row.connection_id

        if connection_id is not None:
            await self.revoke_connection(connection_id)

    async def revoke_connection(self, connection_id: str) -> None:
        """Revokes every OAuth token issued for a connection and marks the
        connection itself revoked. Shared by refresh-token reuse detection,
        explicit /revoke calls, and the dashboard's 'Revoke connection'
        button (see dashboard.py) — harmless (a no-op update/delete) to call
        on a personal-token connection that has no OAuth tokens at all."""
        now = utcnow()
        async with session_scope() as session:
            await session.execute(
                update(OAuthRefreshTokenRow)
                .where(OAuthRefreshTokenRow.connection_id == connection_id, OAuthRefreshTokenRow.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await session.execute(
                delete(OAuthAccessTokenRow).where(OAuthAccessTokenRow.connection_id == connection_id)
            )
            await session.execute(update(Connection).where(Connection.id == connection_id).values(revoked_at=now))
            await session.commit()


oauth_provider = RenderdeskOAuthProvider()


async def sweep_expired_oauth_rows() -> None:
    """Garbage-collects rows nothing else ever cleans up: abandoned/expired
    pending-or-issued authorization codes, expired refresh tokens, and
    registered clients that never completed a single token exchange. Each
    POST /register otherwise permanently adds a row on a disk-constrained
    homelab SQLite file (see DESIGN_NOTES.md). Deletes codes/refresh tokens
    before clients so an unused client is never removed while a (necessarily
    also-expired, since it was never converted into a Connection) row still
    references it — relevant now that PRAGMA foreign_keys is enforced."""
    now = utcnow()
    async with session_scope() as session:
        await session.execute(delete(OAuthAuthorizationCodeRow).where(OAuthAuthorizationCodeRow.expires_at <= now))
        await session.execute(delete(OAuthRefreshTokenRow).where(OAuthRefreshTokenRow.expires_at <= now))
        used_client_ids = select(Connection.client_id).where(Connection.client_id.is_not(None)).distinct()
        await session.execute(
            delete(OAuthClient).where(
                OAuthClient.created_at <= now - UNUSED_CLIENT_TTL,
                OAuthClient.client_id.not_in(used_client_ids),
            )
        )
        await session.commit()
