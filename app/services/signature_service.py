"""Provider-specific webhook signature verification.

Every provider signs differently, so verification sits behind a small interface.
Add a provider by implementing `SignatureVerifier` and registering it in `build_verifier`.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Protocol

from app.config import Settings
from app.core.exceptions import SignatureError, StaleTimestampError
from app.core.security import constant_time_equals, hmac_sha256_hex


class SignatureVerifier(Protocol):
    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        """Raise SignatureError / StaleTimestampError when the request must be rejected."""


class GenericHmacVerifier:
    """HMAC-SHA256 over `"{timestamp}.{raw_body}"`, hex digest (optionally `sha256=` prefixed).

    The timestamp is part of the signed payload, so it cannot be altered to extend a
    captured request's validity; requests outside the tolerance window are rejected.
    """

    def __init__(self, secret: str, signature_header: str, timestamp_header: str, tolerance_seconds: int):
        self.secret = secret
        self.signature_header = signature_header
        self.timestamp_header = timestamp_header
        self.tolerance_seconds = tolerance_seconds

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        signature = headers.get(self.signature_header)
        timestamp = headers.get(self.timestamp_header)
        if not signature or not timestamp:
            raise SignatureError("Missing signature or timestamp header")
        try:
            sent_at = float(timestamp)
        except ValueError:
            raise SignatureError("Invalid timestamp header") from None
        # NaN compares False with everything, so it must be rejected explicitly or it would skip the window check.
        if not math.isfinite(sent_at) or abs(time.time() - sent_at) > self.tolerance_seconds:
            raise StaleTimestampError()
        expected = hmac_sha256_hex(self.secret, timestamp.encode() + b"." + raw_body)
        provided = signature.removeprefix("sha256=").strip().lower()
        if not constant_time_equals(provided, expected):
            raise SignatureError()


class RazorpayVerifier:
    """Razorpay: hex HMAC-SHA256 of the raw body, sent in `X-Razorpay-Signature`.

    Razorpay does not sign a timestamp; replays are neutralised by transaction_id idempotency.
    """

    header = "X-Razorpay-Signature"

    def __init__(self, secret: str):
        self.secret = secret

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> None:
        signature = headers.get(self.header)
        if not signature:
            raise SignatureError("Missing signature header")
        if not constant_time_equals(signature.strip().lower(), hmac_sha256_hex(self.secret, raw_body)):
            raise SignatureError()


def build_verifier(settings: Settings) -> SignatureVerifier | None:
    if not settings.webhook_signature_enabled:
        return None
    if settings.webhook_signature_provider == "razorpay":
        return RazorpayVerifier(settings.webhook_signing_secret)
    return GenericHmacVerifier(
        settings.webhook_signing_secret,
        settings.webhook_signature_header,
        settings.webhook_timestamp_header,
        settings.webhook_timestamp_tolerance_seconds,
    )
