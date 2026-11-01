from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_serializer, field_validator

from app.core.constants import ISO_4217_CODES


Account = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Identifiers are matched exactly as sent: no stripping/normalising of the idempotency key.
    transaction_id: str = Field(pattern=r"^[A-Za-z0-9_\-.:]{1,128}$")
    source_account: Account
    destination_account: Account
    # NUMERIC(18,2) in PostgreSQL: at most 2 decimal places.
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, allow_inf_nan=False)
    currency: str

    @field_validator("source_account", "destination_account")
    @classmethod
    def _printable(cls, value: str) -> str:
        # Rejects NUL / newlines / other control characters (PostgreSQL cannot store NUL; log-injection hygiene).
        if not value.isprintable():
            raise ValueError("must not contain control characters")
        return value

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if code not in ISO_4217_CODES:
            raise ValueError("currency must be a valid ISO 4217 three-letter code")
        return code


class TransactionResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "transaction_id": "txn_abc123def456",
                    "source_account": "acc_user_789",
                    "destination_account": "acc_merchant_456",
                    "amount": 1500,
                    "currency": "INR",
                    "status": "PROCESSED",
                    "created_at": "2024-01-15T10:30:00Z",
                    "processed_at": "2024-01-15T10:30:30Z",
                }
            ]
        }
    )

    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("amount")
    def _amount_as_number(self, value: Decimal) -> int | float:
        return int(value) if value == value.to_integral_value() else float(value)


class WebhookAck(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"status": "accepted", "transaction_id": "txn_abc123def456",
                 "transaction_status": "PROCESSING", "duplicate": False}
            ]
        }
    )

    status: str = "accepted"
    transaction_id: str
    transaction_status: str
    duplicate: bool
