import time
import uuid

from app.database.session import session_scope
from app.services import job_service
from app.workers import heartbeat
from app.workers.job_recovery import fail_exhausted_jobs
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, row, scalar, wait_until


def _claim(worker_id, lease):
    with session_scope("worker") as s:
        return job_service.claim_jobs(s, worker_id, 5, lease)


def test_crashed_worker_job_is_reclaimed_after_lease_expiry(client, db, settings):
    client.post("/v1/webhooks/transactions", json=make_payload())
    dead_worker = uuid.uuid4()
    (job,) = _claim(dead_worker, lease=1)  # claims, then "crashes": never renews or completes
    assert scalar(db, "SELECT status FROM background_jobs") == "PROCESSING"

    assert _claim(uuid.uuid4(), lease=30) == []  # lease still valid -> not reclaimable yet
    time.sleep(1.2)

    survivor = TransactionWorker(settings)
    try:
        assert survivor.poll_once() == 1
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
    finally:
        survivor.shutdown()
    state = row(db, "SELECT attempts, status FROM background_jobs")
    assert (state["attempts"], state["status"]) == (2, "COMPLETED")


def test_stale_lease_token_cannot_complete_reassigned_job(client, db):
    client.post("/v1/webhooks/transactions", json=make_payload())
    (old,) = _claim(uuid.uuid4(), lease=1)
    time.sleep(1.2)
    (new,) = _claim(uuid.uuid4(), lease=30)
    assert new.lease_token != old.lease_token

    with session_scope("worker") as s:
        assert job_service.renew_lease(s, old.job_id, old.lease_token, 30) is False
    with session_scope("worker") as s:
        assert job_service.complete_job(s, old.job_id, old.lease_token) is False
    with session_scope("worker") as s:
        assert job_service.fail_job(s, old.job_id, old.lease_token, "late", 0) is None
    assert scalar(db, "SELECT status FROM transactions") == "PROCESSING"

    with session_scope("worker") as s:
        assert job_service.complete_job(s, new.job_id, new.lease_token) is True
    assert scalar(db, "SELECT status FROM transactions") == "PROCESSED"


def test_worker_heartbeat_does_not_override_job_lease(client, db):
    client.post("/v1/webhooks/transactions", json=make_payload())
    owner = uuid.uuid4()
    heartbeat.register_worker(owner, "host")
    (job,) = _claim(owner, lease=1)
    heartbeat.beat(owner)  # fresh worker heartbeat, but the lease is what counts
    time.sleep(1.2)
    heartbeat.beat(owner)
    assert len(_claim(uuid.uuid4(), lease=30)) == 1  # reclaimed despite a live heartbeat

    # conversely: a valid lease protects a job even when its worker heartbeat is stale
    client.post("/v1/webhooks/transactions", json=make_payload())
    (_,) = _claim(owner, lease=60)
    assert _claim(uuid.uuid4(), lease=30) == []


def test_retry_with_exponential_backoff_then_terminal_failure(client, db, settings):
    client.post("/v1/webhooks/transactions", json=make_payload())

    def failing(job, abort):
        raise RuntimeError("provider timeout")

    worker = TransactionWorker(settings, processor=failing)
    assert worker.backoff_seconds(1) == 0.3 and worker.backoff_seconds(2) == 0.6 and worker.backoff_seconds(3) == 1.2
    try:
        delays = []
        for attempt in (1, 2, 3):
            wait_until(lambda: _eligible(db))
            (job,) = _claim(worker.worker_id, lease=30)
            assert job.attempts == attempt
            worker.process_job(job)
            state = row(db, "SELECT status, last_error, extract(epoch FROM available_at - now()) AS delay FROM background_jobs")
            assert "provider timeout" in state["last_error"]
            delays.append(float(state["delay"]))
            if attempt < 3:
                assert state["status"] == "PENDING"
                assert _claim(worker.worker_id, lease=30) == []  # backoff not elapsed
        assert state["status"] == "FAILED"
        assert delays[0] <= 0.3 < delays[1] <= 0.6  # backoff doubles: 0.3s -> 0.6s (final failure: 1.2s, unused)
    finally:
        worker.shutdown()
    assert scalar(db, "SELECT status FROM transactions") == "FAILED"
    assert scalar(db, "SELECT processed_at FROM transactions") is None
    assert _claim(uuid.uuid4(), lease=30) == []  # terminal


def _eligible(db):
    return scalar(db, "SELECT count(*) FROM background_jobs WHERE status = 'PENDING' AND available_at <= now()") == 1


def test_expired_lease_with_no_attempts_left_fails_terminally(client, db):
    client.post("/v1/webhooks/transactions", json=make_payload())
    with session_scope("worker") as s:
        s.execute(__import__("sqlalchemy").text("UPDATE background_jobs SET max_attempts = 1"))
    _claim(uuid.uuid4(), lease=1)
    time.sleep(1.2)
    with session_scope("worker") as s:
        assert fail_exhausted_jobs(s) == 1
    assert scalar(db, "SELECT status FROM background_jobs") == "FAILED"
    assert scalar(db, "SELECT status FROM transactions") == "FAILED"


def test_pending_jobs_survive_api_restart(db, settings):
    from fastapi.testclient import TestClient
    from app.main import app

    payload = make_payload()
    with TestClient(app) as first:
        assert first.post("/v1/webhooks/transactions", json=payload).status_code == 202
    with TestClient(app) as second:  # "restart": new lifespan, engines disposed and rebuilt
        assert second.get(f"/v1/transactions/{payload['transaction_id']}").json()[0]["status"] == "PROCESSING"
        worker = TransactionWorker(settings)
        try:
            worker.poll_once()
            wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
        finally:
            worker.shutdown()
