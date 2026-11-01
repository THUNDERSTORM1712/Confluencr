from __future__ import annotations

import re

from sqlalchemy import select

from app.config import Settings
from app.core.exceptions import TransactionNotFoundError
from app.database.connection import get_engine, run_with_retry
from app.database.session import session_scope
from app.models.transaction import Transaction
from app.schemas.transaction import TransactionResponse, WebhookPayload
from app.services.idempotency_service import AcceptResult, insert_if_absent

_VALID_ID = re.compile(r"^[A-Za-z0-9_\-.:]{1,128}$")


def accept_transaction(payload: WebhookPayload, settings: Settings) -> AcceptResult:
    """Persist transaction + job atomically. Returns only after the DB commit succeeded.

    The insert is a single SQL statement on an AUTOCOMMIT connection: one network round trip, atomic,
    and committed when it returns. A stale pooled connection is retried once on a fresh connection;
    the insert is idempotent so a retry is safe.
    """

    def _insert() -> AcceptResult:
        with get_engine("api").connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            return insert_if_absent(conn, payload, settings.max_job_attempts)

    return run_with_retry(_insert)


def get_transaction(transaction_id: str) -> TransactionResponse:
    if not _VALID_ID.match(transaction_id):
        # Cannot exist (the webhook validates the same format); also keeps NUL bytes away from PostgreSQL.
        raise TransactionNotFoundError()

    def _select() -> Transaction | None:
        with session_scope("api") as session:
            return session.execute(
                select(Transaction).where(Transaction.transaction_id == transaction_id)
            ).scalar_one_or_none()

    row = run_with_retry(_select)
    if row is None:
        raise TransactionNotFoundError()
    return TransactionResponse(
        transaction_id=row.transaction_id,
        source_account=row.source_account,
        destination_account=row.destination_account,
        amount=row.amount,
        currency=row.currency,
        status=row.status,
        created_at=row.created_at,
        processed_at=row.processed_at,
    )
