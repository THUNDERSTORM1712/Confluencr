"""Database-level idempotency: INSERT ... ON CONFLICT DO NOTHING on transaction_id."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.core.constants import JOB_PENDING, TXN_PROCESSING
from app.schemas.transaction import WebhookPayload

# One statement = one round trip and one atomic unit: the transaction row and its job are inserted
# together or not at all. Run on an AUTOCOMMIT connection, statement completion *is* the commit.
# Concurrent duplicates serialise on the unique index: the loser waits for the winner, then conflicts.
INSERT_BOTH_SQL = text(
    """
    WITH new_txn AS (
        INSERT INTO transactions
            (id, transaction_id, source_account, destination_account, amount, currency, status, correlation_id)
        VALUES
            (:id, :transaction_id, :source_account, :destination_account, :amount, :currency, :status, :correlation_id)
        ON CONFLICT (transaction_id) DO NOTHING
        RETURNING id
    ),
    new_job AS (
        INSERT INTO background_jobs (id, transaction_id, status, attempts, max_attempts)
        SELECT :job_id, id, :job_status, 0, :max_attempts FROM new_txn
        ON CONFLICT (transaction_id) DO NOTHING
        RETURNING id
    )
    SELECT (SELECT count(*) FROM new_txn) AS inserted, (SELECT count(*) FROM new_job) AS jobs
    """
)

SELECT_EXISTING_SQL = text(
    """
    SELECT transaction_id, source_account, destination_account, amount, currency, status, correlation_id
    FROM transactions WHERE transaction_id = :transaction_id
    """
)


@dataclass(frozen=True)
class AcceptResult:
    transaction_id: str
    status: str
    correlation_id: uuid.UUID
    duplicate: bool
    payload_matches: bool = True


def insert_if_absent(conn: Connection, payload: WebhookPayload, max_attempts: int) -> AcceptResult:
    """`conn` must be an AUTOCOMMIT connection (see transaction_service)."""
    correlation_id = uuid.uuid4()
    row = conn.execute(
        INSERT_BOTH_SQL,
        {
            "id": uuid.uuid4(), "transaction_id": payload.transaction_id,
            "source_account": payload.source_account, "destination_account": payload.destination_account,
            "amount": payload.amount, "currency": payload.currency, "status": TXN_PROCESSING,
            "correlation_id": correlation_id, "job_id": uuid.uuid4(), "job_status": JOB_PENDING,
            "max_attempts": max_attempts,
        },
    ).one()
    if row.inserted == 1 and row.jobs == 1:
        return AcceptResult(payload.transaction_id, TXN_PROCESSING, correlation_id, duplicate=False)

    existing = conn.execute(SELECT_EXISTING_SQL, {"transaction_id": payload.transaction_id}).one()
    matches = (
        existing.source_account == payload.source_account
        and existing.destination_account == payload.destination_account
        and existing.amount == payload.amount
        and existing.currency == payload.currency
    )
    return AcceptResult(existing.transaction_id, existing.status, existing.correlation_id, True, matches)
