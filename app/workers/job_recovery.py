"""Housekeeping that complements lease-based reclamation in job_service.claim_jobs.

Expired leases with attempts left are reclaimed directly by the claim query. Here we handle
the terminal case (expired lease, no attempts left) and report workers that stopped heartbeating.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.logging_config import log_event

logger = logging.getLogger("app.worker.recovery")

FAIL_EXHAUSTED_SQL = text(
    """
    WITH failed AS (
        UPDATE background_jobs
        SET status = 'FAILED', last_error = 'lease expired and retry attempts exhausted',
            locked_by = NULL, locked_until = NULL, lease_token = NULL, updated_at = now()
        WHERE status = 'PROCESSING' AND locked_until < now() AND attempts >= max_attempts
        RETURNING transaction_id
    )
    UPDATE transactions SET status = 'FAILED'
    WHERE id IN (SELECT transaction_id FROM failed) AND status = 'PROCESSING'
    RETURNING id
    """
)

STALE_WORKERS_SQL = text(
    """
    UPDATE worker_instances
    SET status = 'STOPPED', stopped_at = now()
    WHERE status = 'RUNNING' AND last_heartbeat_at < now() - make_interval(secs => :stale_seconds)
    RETURNING worker_id, hostname
    """
)


def fail_exhausted_jobs(session: Session) -> int:
    count = len(session.execute(FAIL_EXHAUSTED_SQL).all())
    if count:
        log_event(logger, logging.ERROR, "jobs_failed_after_lease_expiry", detail=count)
    return count


def mark_stale_workers(session: Session, stale_after_seconds: float) -> list[uuid.UUID]:
    """Flag workers whose heartbeat stopped (crash/kill). Does NOT touch their jobs - leases decide that."""
    rows = session.execute(STALE_WORKERS_SQL, {"stale_seconds": stale_after_seconds}).all()
    for row in rows:
        log_event(logger, logging.WARNING, "worker_heartbeat_lost", worker_id=row.worker_id, detail=row.hostname)
    return [r.worker_id for r in rows]
