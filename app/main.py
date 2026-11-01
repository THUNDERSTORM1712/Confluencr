from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import health, transactions, ui, webhooks
from app.config import get_settings
from app.core.exceptions import AppError
from app.database.connection import dispose_engines, get_engine
from app.logging_config import configure_logging, log_event
from app.middleware.correlation import CorrelationMiddleware
from app.services.signature_service import build_verifier

logger = logging.getLogger("app")


def _error(status: int, code: str, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status, headers=headers,
        content={"error": {"code": code, "message": message, **extra}},
    )


def _warm_up_pool(connections: int = 2) -> None:
    """Open a couple of DB connections up front so the first webhook does not pay TCP/TLS/auth setup.

    Failure is non-fatal: /health/ready reports the database state and the pool reconnects lazily.
    """
    try:
        engine = get_engine("api")
        conns = [engine.connect() for _ in range(connections)]
        for conn in conns:
            conn.exec_driver_sql("SELECT 1")
            conn.close()
    except Exception as exc:
        log_event(logger, logging.WARNING, "pool_warmup_failed", error_type=type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()  # raises on invalid configuration -> process fails fast
    configure_logging(settings.app_name, settings.app_env, settings.log_level, settings.log_format)
    _warm_up_pool()
    app.state.ready = True
    embedded_worker, worker_thread = None, None
    if settings.run_worker_in_api:
        # Imported lazily so API-only deployments don't load worker code.
        from app.workers.transaction_worker import TransactionWorker

        embedded_worker = TransactionWorker(settings)
        worker_thread = threading.Thread(target=embedded_worker.run, name="embedded-worker", daemon=True)
        worker_thread.start()
    log_event(
        logger, logging.INFO, "api_started",
        detail=f"signature_verification={app.state.verifier is not None} embedded_worker={embedded_worker is not None}",
    )
    yield
    app.state.ready = False
    if embedded_worker is not None:
        embedded_worker.request_stop()  # in-flight jobs are released; run() then deregisters the worker
        worker_thread.join(timeout=20)
    dispose_engines()
    log_event(logger, logging.INFO, "api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Transaction Webhook Service",
        version="1.0.0",
        description=(
            "Receives payment-processor webhooks, answers **202 Accepted** immediately, and processes each "
            "transaction in the background (30 s simulated work). Duplicate webhooks are safe.\n\n"
            + ("Prefer clicking? Open the **[web UI](/ui)**." if settings.enable_ui else "")
        ),
        openapi_tags=[
            {"name": "Webhooks", "description": "Where the payment provider sends transactions."},
            {"name": "Transactions", "description": "Look up a transaction's processing status."},
            {"name": "Health", "description": "Liveness / readiness probes for monitoring."},
        ],
        debug=False,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.verifier = build_verifier(settings)
    app.state.config_valid = True
    app.state.ready = False
    app.add_middleware(CorrelationMiddleware)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(transactions.router)
    if settings.enable_ui:
        app.include_router(ui.router)

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # FastAPI's errors() has no include_* switches (unlike pydantic's): drop input/ctx/url by hand.
        details = [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]
        return _error(422, "validation_error", "Invalid request", details=details)

    @app.exception_handler(SQLAlchemyError)
    async def _db_error(request: Request, exc: SQLAlchemyError):
        log_event(logger, logging.ERROR, "database_error", error_type=type(exc).__name__)
        return _error(503, "service_unavailable", "Service temporarily unavailable, please retry",
                      headers={"Retry-After": "5"})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        logger.error("unhandled_exception", exc_info=exc, extra={"event": "unhandled_exception"})
        return _error(500, "internal_error", "Internal server error")

    return app


app = create_app()
