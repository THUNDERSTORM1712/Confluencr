from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.config import get_settings
from app.core.exceptions import MalformedRequestError, PayloadTooLargeError, SignatureError
from app.logging_config import bind_context, log_event
from app.schemas.transaction import WebhookAck, WebhookPayload
from app.services.transaction_service import accept_transaction

router = APIRouter(tags=["Webhooks"])
logger = logging.getLogger("app.webhooks")

_EXAMPLE = {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500,
    "currency": "INR",
}


async def _read_body(request: Request, limit: int) -> bytes:
    """Read the raw body (needed for signature checks), refusing anything larger than `limit` bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError()
        chunks.append(chunk)
    return b"".join(chunks)


# The handler reads the raw body itself, so FastAPI cannot infer it: declare it for the docs explicitly.
@router.post(
    "/v1/webhooks/transactions",
    status_code=202,
    response_model=WebhookAck,
    summary="Receive a transaction webhook",
    description=(
        "Validates the webhook, **stores the transaction and its background job in one atomic database "
        "write**, and only then answers `202 Accepted`. The 30-second processing happens afterwards in the "
        "background worker - poll `GET /v1/transactions/{transaction_id}` to follow it.\n\n"
        "Sending the same `transaction_id` again is safe: you get `202` with `duplicate: true` and nothing "
        "is processed twice."
    ),
    responses={
        400: {"description": "The body is not valid JSON (or not a JSON object)."},
        401: {"description": "Signature verification is enabled and the signature/timestamp is missing, wrong or stale."},
        413: {"description": "The body is larger than the configured limit (64 KiB by default)."},
        422: {"description": "Valid JSON, but a field is missing or invalid (amount <= 0, unknown currency, ...)."},
        503: {"description": "The database is unavailable. Nothing was stored - the sender should retry."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": WebhookPayload.model_json_schema(), "example": _EXAMPLE}},
        }
    },
)
async def receive_transaction_webhook(request: Request):
    started = time.perf_counter()
    settings = get_settings()
    raw_body = await _read_body(request, settings.max_request_bytes)

    verifier = request.app.state.verifier
    if verifier is not None:
        try:
            verifier.verify(raw_body, request.headers)
        except SignatureError as exc:
            log_event(logger, logging.WARNING, "webhook_signature_rejected", reason=exc.code)
            raise

    try:
        data = json.loads(raw_body, parse_float=Decimal)  # Decimal keeps money exact (no float rounding)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError; RecursionError = absurd nesting
        raise MalformedRequestError() from None
    if not isinstance(data, dict):
        raise MalformedRequestError("Request body must be a JSON object")

    try:
        payload = WebhookPayload.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        log_event(logger, logging.INFO, "webhook_validation_failed", detail=[e["loc"] for e in errors])
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Invalid webhook payload", "details": errors}},
        )

    bind_context(transaction_id=payload.transaction_id)
    # Blocking DB work runs in a worker thread; 202 is only returned after the write has been committed.
    result = await run_in_threadpool(accept_transaction, payload, settings)
    bind_context(correlation_id=result.correlation_id)

    if not result.duplicate:
        log_event(
            logger, logging.INFO, "webhook_accepted",
            processing_status=result.status, duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    else:
        log_event(logger, logging.INFO, "webhook_duplicate_ignored", processing_status=result.status)
        if not result.payload_matches:
            log_event(logger, logging.WARNING, "webhook_duplicate_payload_mismatch")

    return WebhookAck(
        transaction_id=result.transaction_id, transaction_status=result.status, duplicate=result.duplicate
    )
