"""Job queue operations on PostgreSQL.

Every function is one short DB transaction (the caller's session). No row lock is ever held
while the 30-second processing runs: ownership is expressed by (status, lease_token,
locked_until) columns and every state change is conditional on the lease token (fencing).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

CLAIM_SQL = text(
    """
    WITH next_jobs AS (
        SELECT id
        FROM background_jobs
        WHERE attempts < max_attempts
          AND (
                (status = 'PENDING' AND available_at <= now())
             OR (status = 'PROCESSING' AND locked_until < now())   -- expired lease: crashed worker
          )
        ORDER BY available_at, created_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE background_jobs j
    SET status = 'PROCESSING',
        attempts = j.attempts + 1,
        locked_by = :worker_id,
        locked_until = now() + make_interval(secs => :lease_seconds),
        lease_token = gen_random_uuid(),
        last_heartbeat_at = now(),
        updated_at = now()
    FROM next_jobs
    WHERE j.id = next_jobs.id
    RETURNING j.id AS job_id, j.transaction_id AS txn_pk, j.lease_token, j.attempts, j.max_attempts
    """
)

MARK_TXN_PROCESSING_SQL = text(
    """
    UPDATE transactions
    SET status = 'PROCESSING'
    WHERE id IN :ids AND status <> 'PROCESSED'
    RETURNING id, transaction_id, correlation_id
    """
).bindparams(bindparam("ids", expanding=True))

RENEW_SQL = text(
    """
    UPDATE background_jobs
    SET locked_until = now() + make_interval(secs => :lease_seconds),
        last_heartbeat_at = now(), updated_at = now()
    WHERE id = :job_id AND status = 'PROCESSING' AND lease_token = :token
    """
)

COMPLETE_SQL = text(
    """
    WITH done AS (
        UPDATE background_jobs
        SET status = 'COMPLETED', completed_at = now(), updated_at = now(),
            locked_by = NULL, locked_until = NULL, lease_token = NULL
        WHERE id = :job_id AND status = 'PROCESSING' AND lease_token = :token
        RETURNING transaction_id
    )
    UPDATE transactions
    SET status = 'PROCESSED', processed_at = now()
    WHERE id IN (SELECT transaction_id FROM done) AND status <> 'PROCESSED'
    RETURNING id
    """
)

FAIL_SQL = text(
    """
    UPDATE background_jobs
    SET status = CASE WHEN attempts >= max_attempts THEN 'FAILED' ELSE 'PENDING' END,
        available_at = now() + make_interval(secs => :backoff_seconds),
        last_error = :error,
        locked_by = NULL, locked_until = NULL, lease_token = NULL, updated_at = now()
    WHERE id = :job_id AND status = 'PROCESSING' AND lease_token = :token
    RETURNING status, transaction_id
    """
)

RELEASE_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'PENDING', attempts = GREATEST(attempts - 1, 0), available_at = now(),
        locked_by = NULL, locked_until = NULL, lease_token = NULL, updated_at = now()
    WHERE id = :job_id AND status = 'PROCESSING' AND lease_token = :token
    """
)


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    txn_pk: uuid.UUID
    transaction_id: str
    correlation_id: uuid.UUID
    lease_token: uuid.UUID
    attempts: int
    max_attempts: int


def claim_jobs(session: Session, worker_id: uuid.UUID, limit: int, lease_seconds: float) -> list[ClaimedJob]:
    """Atomically claim up to `limit` eligible jobs (FOR UPDATE SKIP LOCKED) and mark their transactions."""
    rows = session.execute(
        CLAIM_SQL, {"limit": limit, "worker_id": worker_id, "lease_seconds": lease_seconds}
    ).all()
    if not rows:
        return []
    txns = {
        r.id: r
        for r in session.execute(MARK_TXN_PROCESSING_SQL, {"ids": [r.txn_pk for r in rows]}).all()
    }
    return [
        ClaimedJob(
            job_id=r.job_id, txn_pk=r.txn_pk, transaction_id=txns[r.txn_pk].transaction_id,
            correlation_id=txns[r.txn_pk].correlation_id, lease_token=r.lease_token,
            attempts=r.attempts, max_attempts=r.max_attempts,
        )
        for r in rows
        if r.txn_pk in txns
    ]


def renew_lease(session: Session, job_id: uuid.UUID, token: uuid.UUID, lease_seconds: float) -> bool:
    return session.execute(
        RENEW_SQL, {"job_id": job_id, "token": token, "lease_seconds": lease_seconds}
    ).rowcount == 1


def complete_job(session: Session, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Mark job COMPLETED and transaction PROCESSED in one statement; False if the lease was lost."""
    return session.execute(COMPLETE_SQL, {"job_id": job_id, "token": token}).first() is not None


def fail_job(
    session: Session, job_id: uuid.UUID, token: uuid.UUID, error: str, backoff_seconds: float
) -> str | None:
    """Schedule a retry (PENDING + backoff) or terminally FAIL. Returns the new job status, or None if lease lost."""
    row = session.execute(
        FAIL_SQL, {"job_id": job_id, "token": token, "error": error[:2000], "backoff_seconds": backoff_seconds}
    ).first()
    if row is None:
        return None
    if row.status == "FAILED":
        session.execute(
            text("UPDATE transactions SET status = 'FAILED' WHERE id = :id AND status = 'PROCESSING'"),
            {"id": row.transaction_id},
        )
    return row.status


def release_job(session: Session, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    """Give an unfinished job back (graceful shutdown) without consuming a retry attempt."""
    return session.execute(RELEASE_SQL, {"job_id": job_id, "token": token}).rowcount == 1
