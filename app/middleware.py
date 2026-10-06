"""Bound request bytes before multipart parsing, including chunked uploads."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class UploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        async def too_large() -> None:
            response = JSONResponse(
                {
                    "detail": {
                        "code": "upload_too_large",
                        "message": "Request body exceeds the upload limit.",
                    }
                },
                status_code=413,
            )
            await response(scope, receive, send)

        content_length = dict(scope.get("headers", [])).get(b"content-length")
        if content_length:
            try:
                if int(content_length) > self.max_body_bytes:
                    await too_large()
                    return
            except ValueError:
                pass  # The byte counter below remains authoritative.

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_body_bytes:
                await too_large()
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
