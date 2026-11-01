from app.config import Settings
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, scalar, wait_until


def test_get_unknown_transaction_404(client):
    res = client.get("/v1/transactions/does_not_exist")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "transaction_not_found"


def test_status_lifecycle_processing_then_processed(client, db, settings):
    payload = make_payload(amount=150.5, currency="USD")
    client.post("/v1/webhooks/transactions", json=payload)

    (item,) = client.get(f"/v1/transactions/{payload['transaction_id']}").json()
    assert item["status"] == "PROCESSING"
    assert item["processed_at"] is None
    assert item["amount"] == 150.5 and item["currency"] == "USD"
    assert item["source_account"] == payload["source_account"]

    worker = TransactionWorker(settings)  # PROCESSING_DELAY_SECONDS=1 in tests
    try:
        worker.poll_once()
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
    finally:
        worker.shutdown()

    (done,) = client.get(f"/v1/transactions/{payload['transaction_id']}").json()
    assert done["status"] == "PROCESSED"
    assert done["processed_at"] is not None and done["created_at"] is not None
    elapsed = scalar(db, "SELECT extract(epoch FROM processed_at - created_at) FROM transactions")
    assert elapsed >= 1.0  # configured test delay


def test_integer_amount_rendered_as_number(client):
    payload = make_payload(amount=1500)
    client.post("/v1/webhooks/transactions", json=payload)
    assert client.get(f"/v1/transactions/{payload['transaction_id']}").json()[0]["amount"] == 1500


def test_production_delay_is_30_seconds_and_cannot_be_shortened():
    import pytest
    from pydantic import ValidationError

    base = dict(database_url="postgresql://u:p@h/db", webhook_signature_enabled=True, webhook_signing_secret="s")
    assert Settings.model_fields["processing_delay_seconds"].default == 30  # tests override via env only
    with pytest.raises(ValidationError):
        Settings(app_env="production", processing_delay_seconds=1, **base)
    assert Settings(app_env="production", processing_delay_seconds=30, **base).processing_delay_seconds == 30
