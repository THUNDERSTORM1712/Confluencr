"""Pure-ASGI middleware: request id propagation, response header, request log line."""
from __future__ import annotations

import logging
import re
import time
import uuid

from app.core.constants import REQUEST_ID_HEADER
from app.logging_config import bind_context, clear_context, log_event

logger = logging.getLogger("app.request")
_VALID_ID = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")
_QUIET_PATHS = {"/", "/health/live", "/health/ready"}


class CorrelationMiddleware:
    def __init__(self, app):
        self.app = app
        self.header_key = REQUEST_ID_HEADER.lower().encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = next((v.decode("latin-1") for k, v in scope["headers"] if k == self.header_key), None)
        request_id = incoming if incoming and _VALID_ID.match(incoming) else str(uuid.uuid4())
        clear_context()
        bind_context(request_id=request_id)
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message["headers"] = [*message.get("headers", []), (self.header_key, request_id.encode())]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            level = logging.DEBUG if scope["path"] in _QUIET_PATHS else logging.INFO
            log_event(
                logger, level, "request_completed",
                method=scope["method"], path=scope["path"], status_code=status["code"],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
