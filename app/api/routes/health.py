from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.constants import REQUIRED_SCHEMA_VERSION
from app.database.connection import get_engine, run_with_retry
from app.logging_config import log_event

router = APIRouter(tags=["Health"])
logger = logging.getLogger("app.health")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/", summary="Health check", description="Returns `HEALTHY` while the application is running.")
def root():
    return {"status": "HEALTHY", "current_time": _now_iso()}


@router.get("/health/live", summary="Liveness probe")
def live():
    """Process liveness only - deliberately does not touch the database."""
    return {"status": "ALIVE", "current_time": _now_iso()}


def _schema_version(conn) -> int:
    rows = conn.execute(text("SELECT version FROM flyway_schema_history WHERE success AND version IS NOT NULL"))
    versions = [int(r[0]) for r in rows if str(r[0]).isdigit()]
    return max(versions, default=0)


@router.get(
    "/health/ready",
    summary="Readiness probe",
    description="`200` when the app is initialised, configured, can reach the database and the schema is up to date; otherwise `503`.",
)
def ready(request: Request):
    checks = {"initialized": "ok" if getattr(request.app.state, "ready", False) else "failed"}
    checks["configuration"] = "ok" if getattr(request.app.state, "config_valid", False) else "failed"
    def _probe() -> int:
        with get_engine("api").connect() as conn:
            conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
            return _schema_version(conn)

    try:
        version = run_with_retry(_probe)
        checks["schema"] = "ok" if version >= REQUIRED_SCHEMA_VERSION else "outdated"
    except Exception as exc:  # no connection details in the response
        log_event(logger, logging.ERROR, "readiness_check_failed", error_type=type(exc).__name__)
        checks.setdefault("database", "unavailable")
        checks.setdefault("schema", "unknown")
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "READY" if healthy else "NOT_READY", "current_time": _now_iso(), "checks": checks},
    )
