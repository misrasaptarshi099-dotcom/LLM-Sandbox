"""Standardized API exception handlers.

Ensures consistent JSON error envelopes: {"error": {"code": "...", "message": "..."}}.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger("errors")


def setup_exception_handlers(app: FastAPI) -> None:
    """Attach standard error handlers to FastAPI application."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = "HTTP_ERROR"
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            code = "UNAUTHORIZED"
        elif exc.status_code == status.HTTP_403_FORBIDDEN:
            code = "FORBIDDEN"
        elif exc.status_code == status.HTTP_404_NOT_FOUND:
            code = "NOT_FOUND"
        elif exc.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT:
            code = "VALIDATION_ERROR"
        elif exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
            code = "RATE_LIMIT_EXCEEDED"

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": str(exc.detail),
                }
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        first_msg = errors[0]["msg"] if errors else "Invalid request format"
        loc = errors[0].get("loc", []) if errors else []
        field = " -> ".join(str(item) for item in loc)
        message = f"{field}: {first_msg}" if field else first_msg

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": message,
                }
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(f"Unhandled server error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "An internal server error occurred. Please try again later.",
                }
            },
        )
