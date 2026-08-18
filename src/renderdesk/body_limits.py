from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class _BodyTooLargeError(Exception):
    pass


class MaxBodySizeMiddleware:
    """Rejects oversized request bodies before anything downstream reads
    them into memory — nothing else in this app enforces a request-body
    cap, and the SDK/framework layers behind /mcp and /register buffer the
    whole body before any application-level size check would otherwise
    run. Raw ASGI rather than BaseHTTPMiddleware so the cap is enforced on
    the byte stream itself
    (via a wrapped `receive`), not just on a `Content-Length` header a
    chunked-encoded or lying request could omit or understate.

    `limits` is (path_prefix, max_bytes) pairs, first match wins — add it
    as the *last* app.add_middleware(...) call so it ends up outermost
    (Starlette wraps middleware in reverse add order) and rejects before
    CSRF/rate-limit/etc middleware get a chance to touch the body."""

    def __init__(self, app: ASGIApp, limits: list[tuple[str, int]]) -> None:
        self.app = app
        self.limits = limits

    def _limit_for(self, path: str) -> int | None:
        for prefix, max_bytes in self.limits:
            if path.startswith(prefix):
                return max_bytes
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self._limit_for(scope["path"])
        if max_bytes is None:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > max_bytes:
                await _reject_too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _BodyTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await _reject_too_large(scope, receive, send)


async def _reject_too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"error": "payload_too_large"}, status_code=413)
    await response(scope, receive, send)
