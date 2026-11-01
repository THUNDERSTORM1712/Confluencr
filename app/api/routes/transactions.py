from __future__ import annotations

from fastapi import APIRouter, Path

from app.schemas.transaction import TransactionResponse
from app.services.transaction_service import get_transaction

router = APIRouter(tags=["Transactions"])


@router.get(
    "/v1/transactions/{transaction_id}",
    response_model=list[TransactionResponse],
    summary="Get a transaction's status",
    description=(
        "Returns the transaction as a one-element array (the format the assessment specifies).\n\n"
        "`status` is `PROCESSING` until the background job finishes (about 30 seconds), then `PROCESSED` "
        "with `processed_at` filled in. `FAILED` appears only if processing kept failing after all retries."
    ),
    responses={404: {"description": "No transaction with this ID exists."}},
)
def get_transaction_status(transaction_id: str = Path(max_length=128, description="The `transaction_id` sent in the webhook.", examples=["txn_abc123def456"])):
    return [get_transaction(transaction_id)]
