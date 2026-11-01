import io
import json
import logging
import re
import uuid

from app.logging_config import JsonFormatter
from tests.conftest import make_payload, scalar


def test_valid_request_id_is_echoed(client):
    assert client.get("/", headers={"X-Request-ID": "req-abc_123"}).headers["x-request-id"] == "req-abc_123"


def test_missing_or_invalid_request_id_replaced_with_uuid(client):
    generated = client.get("/").headers["x-request-id"]
    uuid.UUID(generated)
    bad = client.get("/", headers={"X-Request-ID": "bad id!\t"}).headers["x-request-id"]
    uuid.UUID(bad)


def test_api_logs_carry_request_and_transaction_correlation(client, db):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test", "test"))
    log = logging.getLogger("app.webhooks")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        payload = make_payload()
        res = client.post("/v1/webhooks/transactions", json=payload, headers={"X-Request-ID": "trace-1"})
    finally:
        log.removeHandler(handler)

    assert res.headers["x-request-id"] == "trace-1"
    entry = next(json.loads(l) for l in stream.getvalue().splitlines() if "webhook_accepted" in l)
    stored = str(scalar(db, "SELECT correlation_id FROM transactions"))
    assert entry["request_id"] == "trace-1"
    assert entry["correlation_id"] == stored
    assert entry["transaction_id"] == payload["transaction_id"]
    assert re.fullmatch(r"[0-9a-f-]{36}", stored)
