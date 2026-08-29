"""HTTP Middleware for request security and boundary limits.

Architecture §5, PRD §11:
- Protects the API server from unbounded memory consumption and body bombs.
- Rejects oversized requests with HTTP 413 Request Entity Too Large.
"""

from __future__ import annotations

from fastapi import status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests with bodies exceeding max_request_body_bytes."""

    def __init__(self, app, max_bytes: int | None = None) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        max_bytes = self.max_bytes or get_settings().max_request_body_bytes

        # 1. Fast check via Content-Length header
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
                if length > max_bytes:
                    return JSONResponse(
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
            except ValueError:
                pass

        # 2. For requests that send bodies, verify actual received bytes
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if len(body) > max_bytes:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={
                        "error": {
                            "code": "REQUEST_ENTITY_TOO_LARGE",
                            "message": (
                                f"Request body exceeds maximum size of {max_bytes} bytes "
                                f"({len(body)} bytes received)."
                            ),
                        }
                    },
                )

        return await call_next(request)
