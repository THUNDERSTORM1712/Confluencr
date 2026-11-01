"""SQLAlchemy engines: one bounded pool for the API, a separate one for the worker."""
from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DBAPIError

from app.config import Settings, get_settings

T = TypeVar("T")
_engines: dict[str, Engine] = {}


def build_engine(settings: Settings, role: str) -> Engine:
    if role == "worker":
        pool_size, overflow = settings.worker_pool_size, settings.worker_max_overflow
    else:
        pool_size, overflow = settings.api_pool_size, settings.api_max_overflow
    return create_engine(
        settings.sqlalchemy_url,
        pool_size=pool_size,
        max_overflow=overflow,
        pool_timeout=settings.api_pool_timeout_seconds,
        pool_recycle=settings.pool_recycle_seconds,
        # API: no per-checkout ping (it costs a network round trip per request); stale connections are
        # recycled and API DB calls go through run_with_retry. Worker: latency is irrelevant, so ping
        # to avoid a stale connection wrongly burning a job retry attempt.
        pool_pre_ping=(role == "worker"),
        connect_args={
            "sslmode": settings.database_ssl_mode,
            # Compatible with Supabase's transaction pooler (no server-side prepared statements).
            "prepare_threshold": None,
            "connect_timeout": 5,
            "application_name": f"{settings.app_name}-{role}",
        },
    )


def run_with_retry(fn: Callable[[], T]) -> T:
    """Run an idempotent DB operation, retrying once if the pooled connection turned out to be stale."""
    try:
        return fn()
    except DBAPIError as exc:
        if not exc.connection_invalidated:
            raise
        return fn()


def get_engine(role: str = "api") -> Engine:
    if role not in _engines:
        _engines[role] = build_engine(get_settings(), role)
    return _engines[role]


def dispose_engines() -> None:
    for engine in _engines.values():
        engine.dispose()
    _engines.clear()
