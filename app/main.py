"""LLM Sandbox — FastAPI application entrypoint.

Responsibilities:
- Mount route modules.
- Configure structured logging with redaction.
- Set up lifespan events for startup/shutdown.

This module handles HTTP concerns only (Rule §6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api.errors import setup_exception_handlers
from app.api.middleware import RequestSizeLimitMiddleware
from app.api.routes import challenges, health, runs
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan — startup and shutdown hooks."""
    settings = get_settings()
    setup_logging(level=settings.log_level)
    logger = get_logger("main")
    logger.info("LLM Sandbox starting", extra={"extra_fields": {"env": settings.app_env}})

    # Auto-initialize database schema and seed challenge if needed on startup
    try:
        from scripts.seed_challenge import seed

        await seed()
        logger.info("Database schema and challenge data verified/initialized")
    except Exception as exc:
        logger.warning(
            "Database auto-init notice",
            extra={"extra_fields": {"notice": str(exc)}},
        )

    worker_task = None
    if settings.embedded_worker:
        import asyncio

        from app.worker import run_worker

        worker_task = asyncio.create_task(run_worker(max_concurrent=10))
        logger.info("Embedded background worker started successfully")

    yield

    if worker_task is not None:
        worker_task.cancel()
        logger.info("Embedded background worker stopped")

    logger.info("LLM Sandbox shutting down")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()
    app = FastAPI(
        title="LLM Sandbox",
        description="Prompt Injection Challenge Backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect bare root to interactive Swagger UI documentation."""
        return RedirectResponse(url="/docs")

    # Configure trusted proxy headers handling at ASGI boundary
    app.add_middleware(
        ProxyHeadersMiddleware,
        trusted_hosts=settings.trusted_proxies,
    )
    # Enable CORS for browser fetch requests and Swagger UI
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Enforce request body size limit before route dispatch
    app.add_middleware(RequestSizeLimitMiddleware)

    # Register exception handlers
    setup_exception_handlers(app)

    # Mount API routers
    app.include_router(health.router)
    app.include_router(challenges.router)
    app.include_router(runs.router)
    return app


app = create_app()
