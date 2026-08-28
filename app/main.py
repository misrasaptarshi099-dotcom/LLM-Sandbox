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

from app.api.routes import health
from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan — startup and shutdown hooks."""
    settings = get_settings()
    setup_logging(level=settings.log_level)
    logger = get_logger("main")
    logger.info("LLM Sandbox starting", extra={"extra_fields": {"env": settings.app_env}})
    yield
    logger.info("LLM Sandbox shutting down")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="LLM Sandbox",
        description="Prompt Injection Challenge Backend — TechnoVIT / GDG VIT Chennai",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    return app


app = create_app()
