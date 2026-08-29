"""HTTP Middleware for request security and boundary limits.

Architecture §5, PRD §11:
- Protects the API server from unbounded memory consumption and body bombs.
- Rejects oversized requests with HTTP 413 Content Too Large.
- Streams and counts incoming ASGI http.request chunks without buffering unbounded payloads.
"""

from __future__ import annotations

from fastapi import status
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import get_settings


class RequestSizeLimitMiddleware:
    """Pure ASGI middleware enforcing max_request_body_bytes.

    Aborts and returns HTTP 413 immediately as soon as max_bytes is exceeded
    while consuming incoming chunks, avoiding memory buffering of unbounded payloads.
    """

    def __init__(self, app: ASGIApp, max_bytes: int | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_bytes = self.max_bytes or get_settings().max_request_body_bytes

        # 1. Fast check via Content-Length header
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > max_bytes:
                    response = JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "error": {
                                "code": "REQUEST_ENTITY_TOO_LARGE",
                                "message": (
                                    f"Request body exceeds maximum size of {max_bytes} bytes "
                                    f"({length} bytes indicated)."
                                ),
                            }
                        },
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        # 2. Count incoming ASGI http.request chunks while consuming without unbounded buffering
        method = scope.get("method", "")
        if method in ("POST", "PUT", "PATCH"):
            chunks: list[bytes] = []
            bytes_received = 0
            more_body = True

            while more_body:
                msg = await receive()
                if msg["type"] == "http.request":
                    chunk = msg.get("body", b"")
                    bytes_received += len(chunk)
                    if bytes_received > max_bytes:
                        # Abort immediately upon exceeding limit; do not buffer remaining stream
                        response = JSONResponse(
                            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                            content={
                                "error": {
                                    "code": "REQUEST_ENTITY_TOO_LARGE",
                                    "message": (
                                        f"Request body exceeds maximum size of {max_bytes} bytes "
                                        f"({bytes_received} bytes received)."
                                    ),
                                }
                            },
                        )
                        await response(scope, receive, send)
                        return

                    chunks.append(chunk)
                    more_body = msg.get("more_body", False)
                elif msg["type"] == "http.disconnect":
                    return

            # Replay validated body chunks to downstream application
            full_body = b"".join(chunks)
            sent = False

            async def cached_receive() -> dict:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": full_body, "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, cached_receive, send)
            return

        await self.app(scope, receive, send)
