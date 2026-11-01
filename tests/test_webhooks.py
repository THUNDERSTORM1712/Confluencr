import time

import pytest
from sqlalchemy import text

from tests.conftest import make_payload, scalar


def test_valid_webhook_accepted_fast_and_persisted(client, db):
    client.post("/v1/webhooks/transactions", json=make_payload())  # warm the connection pool
    payload = make_payload()
    started = time.perf_counter()
    res = client.post("/v1/webhooks/transactions", json=payload)
    elapsed = time.perf_counter() - started

    assert res.status_code == 202
    assert elapsed < 0.5
    assert res.json()["duplicate"] is False
    assert scalar(db, "SELECT count(*) FROM transactions WHERE transaction_id = :t", t=payload["transaction_id"]) == 1
    job = scalar(
        db,
        "SELECT j.status FROM background_jobs j JOIN transactions t ON t.id = j.transaction_id "
        "WHERE t.transaction_id = :t",
        t=payload["transaction_id"],
    )
    assert job == "PENDING"


def test_atomic_persistence_when_job_insert_fails(client, db):
    with db.begin() as conn:
        conn.execute(text("CREATE OR REPLACE FUNCTION fail_job() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'boom'; END $$ LANGUAGE plpgsql"))
        conn.execute(text("CREATE TRIGGER fail_job_insert BEFORE INSERT ON background_jobs FOR EACH ROW EXECUTE FUNCTION fail_job()"))
    payload = make_payload()
    res = client.post("/v1/webhooks/transactions", json=payload)

    assert res.status_code == 503  # never a false 202
    assert res.headers.get("retry-after")
    assert scalar(db, "SELECT count(*) FROM transactions") == 0
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 0

    with db.begin() as conn:
        conn.execute(text("DROP TRIGGER fail_job_insert ON background_jobs"))
    assert client.post("/v1/webhooks/transactions", json=payload).status_code == 202  # provider retry succeeds


def test_malformed_json_rejected(client):
    res = client.post("/v1/webhooks/transactions", content=b"{not json", headers={"content-type": "application/json"})
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "malformed_request"


def test_non_object_json_rejected(client):
    assert client.post("/v1/webhooks/transactions", json=[1, 2]).status_code == 400


@pytest.mark.parametrize(
    "override",
    [
        {"amount": 0},
        {"amount": -5},
        {"amount": 10.123},
        {"amount": "abc"},
        {"currency": "XYZ"},
        {"currency": "INRR"},
        {"currency": "12"},
        {"source_account": ""},
        {"destination_account": None},
        {"transaction_id": "bad id with spaces"},
        {"transaction_id": ""},
    ],
)
def test_invalid_payload_rejected(client, override):
    res = client.post("/v1/webhooks/transactions", json=make_payload(**override))
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "validation_error"


def test_missing_field_rejected(client):
    body = make_payload()
    del body["currency"]
    assert client.post("/v1/webhooks/transactions", json=body).status_code == 422


def test_lowercase_currency_normalised(client, db):
    body = make_payload(currency="usd")
    assert client.post("/v1/webhooks/transactions", json=body).status_code == 202
    assert scalar(db, "SELECT currency FROM transactions WHERE transaction_id = :t", t=body["transaction_id"]) == "USD"


def test_decimal_amount_stored_exactly(client, db):
    body = make_payload(amount=150.50)
    client.post("/v1/webhooks/transactions", json=body)
    assert str(scalar(db, "SELECT amount FROM transactions WHERE transaction_id = :t", t=body["transaction_id"])) == "150.50"


def test_oversized_body_rejected(client):
    res = client.post("/v1/webhooks/transactions", content=b"x" * 70000, headers={"content-type": "application/json"})
    assert res.status_code == 413
