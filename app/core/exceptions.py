"""Application exceptions mapped to HTTP responses in app.main."""


class AppError(Exception):
    status_code = 500
    code = "internal_error"
    message = "Internal server error"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.message)
        self.message = message or self.message


class MalformedRequestError(AppError):
    status_code = 400
    code = "malformed_request"
    message = "Request body is not valid JSON"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "Request body too large"


class SignatureError(AppError):
    status_code = 401
    code = "invalid_signature"
    message = "Webhook signature verification failed"


class StaleTimestampError(SignatureError):
    code = "stale_timestamp"
    message = "Webhook timestamp outside the accepted window"


class TransactionNotFoundError(AppError):
    status_code = 404
    code = "transaction_not_found"
    message = "Transaction not found"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    message = "Service temporarily unavailable, please retry"
