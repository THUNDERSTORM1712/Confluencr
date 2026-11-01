from fastapi.testclient import TestClient

import app.main as main
from tests.conftest import make_payload, scalar, wait_until


def test_embedded_worker_processes_and_stops_cleanly(monkeypatch, settings, db):
    cfg = settings.model_copy(update={"run_worker_in_api": True})
    monkeypatch.setattr(main, "get_settings", lambda: cfg)
    payload = make_payload()
    with TestClient(main.create_app()) as client:
        assert client.post("/v1/webhooks/transactions", json=payload).status_code == 202
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED", timeout=15)
        assert client.get(f"/v1/transactions/{payload['transaction_id']}").json()[0]["status"] == "PROCESSED"
        assert scalar(db, "SELECT status FROM worker_instances") == "RUNNING"
    assert scalar(db, "SELECT status FROM worker_instances") == "STOPPED"  # deregistered on shutdown
