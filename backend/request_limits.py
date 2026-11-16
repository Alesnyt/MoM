"""Bound ordinary API bodies before FastAPI parses JSON or validates models."""
from collections.abc import Awaitable, Callable

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


AUTH_BODY_BYTES = 16 * 1024
API_BODY_BYTES = 1024 * 1024
AUTH_ACTIONS = {
    "/api/auth/setup": "setup",
    "/api/auth/login": "admin",
    "/api/session/login": "user",
}
# These handlers authenticate before reading, then use upload.bounded_form.
# Exempt only their actual POST routes, irrespective of client Content-Type.
STREAMED_FORMS = {
    "/api/meetings",
    "/api/meetings/transcript",
    "/api/admin/context/import",
    "/api/admin/glossaries/preview",
}


class RequestLimitsMiddleware:
    def __init__(self, app: ASGIApp, *, rate_check: Callable[[Request, str], Awaitable[None]]):
        self.app = app
        self.rate_check = rate_check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        method = scope.get("method", "")
        if scope["type"] != "http" or not path.startswith("/api/") or method not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        action = AUTH_ACTIONS.get(path.rstrip("/")) if method == "POST" else None
        if action:
            try:
                await self.rate_check(request, action)
            except HTTPException as exc:
                await JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                                   headers=exc.headers)(scope, receive, send)
                return
        if method == "POST" and path in STREAMED_FORMS:
            await self.app(scope, receive, send)
            return

        limit = AUTH_BODY_BYTES if action else API_BODY_BYTES
        too_large = JSONResponse({"detail": f"Запрос превышает допустимый размер ({limit // 1024} КБ)"}, status_code=413)
        declared = request.headers.get("content-length")
        if declared is not None:
            # Avoid converting unbounded attacker-controlled decimal strings.
            normalized = declared.lstrip("0") or "0"
            if not declared.isascii() or not declared.isdigit():
                await JSONResponse({"detail": "Некорректный размер запроса"}, status_code=400)(scope, receive, send)
                return
            if len(normalized) > len(str(limit)) or int(normalized) > limit:
                await too_large(scope, receive, send)
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await too_large(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        # Buffer only bounded bodies, so parser errors cannot swallow a size
        # exception and turn a streaming overflow into a misleading HTTP 400.
        replayed = False

        async def bounded_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)
