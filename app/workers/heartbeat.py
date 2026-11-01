"""Worker registry + periodic worker heartbeat. Informational: job leases stay authoritative."""
from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy import text

from app.core.constants import WORKER_RUNNING, WORKER_STOPPED
from app.database.session import session_scope
from app.logging_config import log_event

logger = logging.getLogger("app.worker.heartbeat")


def register_worker(worker_id: uuid.UUID, hostname: str) -> None:
    with session_scope("worker") as s:
        s.execute(
            text(
                """INSERT INTO worker_instances (worker_id, hostname, status)
                   VALUES (:id, :host, :status)
                   ON CONFLICT (worker_id) DO UPDATE
                   SET status = :status, last_heartbeat_at = now(), stopped_at = NULL"""
            ),
            {"id": worker_id, "host": hostname, "status": WORKER_RUNNING},
        )


def beat(worker_id: uuid.UUID) -> None:
    with session_scope("worker") as s:
        s.execute(
            text(
                """UPDATE worker_instances
                   SET last_heartbeat_at = now(), status = :status, stopped_at = NULL
                   WHERE worker_id = :id"""
            ),
            {"id": worker_id, "status": WORKER_RUNNING},
        )


def deregister_worker(worker_id: uuid.UUID) -> None:
    with session_scope("worker") as s:
        s.execute(
            text("UPDATE worker_instances SET status = :status, stopped_at = now() WHERE worker_id = :id"),
            {"id": worker_id, "status": WORKER_STOPPED},
        )


class HeartbeatThread(threading.Thread):
    def __init__(self, worker_id: uuid.UUID, interval_seconds: float):
        super().__init__(name="worker-heartbeat", daemon=True)
        self.worker_id = worker_id
        self.interval = interval_seconds
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                beat(self.worker_id)
            except Exception as exc:  # never kill the thread; the next tick retries
                log_event(logger, logging.ERROR, "worker_heartbeat_failed",
                          worker_id=self.worker_id, error_type=type(exc).__name__)

    def stop(self) -> None:
        self._stop_event.set()
