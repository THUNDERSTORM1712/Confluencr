"""Structured JSON logging with context-var based correlation fields."""
from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Context carried across a request (API) or a job (worker).
_context: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})

CONTEXT_FIELDS = ("request_id", "correlation_id", "transaction_id", "worker_id", "job_id")
EXTRA_FIELDS = (
    "processing_status", "duration_ms", "error_type", "attempt", "status_code",
    "method", "path", "reason", "detail",
)
_SENSITIVE = ("password", "secret", "authorization", "signature", "token")


def bind_context(**fields: Any) -> None:
    _context.set({**_context.get(), **{k: str(v) for k, v in fields.items() if v is not None}})


def clear_context() -> None:
    _context.set({})


def get_context() -> dict[str, Any]:
    return dict(_context.get())


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str, environment: str):
        super().__init__()
        self.service_name = service_name
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "log_level": record.levelname,
            "service_name": self.service_name,
            "environment": self.environment,
            "event": getattr(record, "event", record.getMessage()),
        }
        entry.update(get_context())
        for name in EXTRA_FIELDS:
            if hasattr(record, name):
                entry[name] = getattr(record, name)
        # Explicit per-call context overrides ambient context.
        for name in CONTEXT_FIELDS:
            if hasattr(record, name):
                entry[name] = str(getattr(record, name))
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = entry.get("error_type") or record.exc_info[0].__name__
            entry["stack"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in entry.items() if v is not None}, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {n: getattr(record, n) for n in EXTRA_FIELDS + CONTEXT_FIELDS if hasattr(record, n)}
        return f"{record.levelname} {getattr(record, 'event', record.getMessage())} {get_context() | extras}"


def configure_logging(service_name: str, environment: str, level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name, environment) if fmt == "json" else _TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # uvicorn's plain-text access log is replaced by our structured request log line.
    access = logging.getLogger("uvicorn.access")
    access.handlers[:] = []
    access.propagate = False


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Emit a structured event. Keys that look sensitive are dropped defensively."""
    safe = {k: v for k, v in fields.items() if not any(s in k.lower() for s in _SENSITIVE)}
    logger.log(level, event, extra={"event": event, **safe})
