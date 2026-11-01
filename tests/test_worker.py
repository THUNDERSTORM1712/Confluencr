import io
import json
import logging
import time

from app.logging_config import JsonFormatter
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, row, scalar, wait_until


def test_transaction_enters_processing_then_processed(client, db, settings):
    payload = make_payload()
    client.post("/v1/webhooks/transactions", json=payload)
    slow = settings.model_copy(update={"processing_delay_seconds": 1.5})
    worker = TransactionWorker(slow)
    try:
        assert worker.poll_once() == 1
        mid = row(db, "SELECT t.status AS t, j.status AS j, j.locked_by, j.lease_token FROM transactions t "
                      "JOIN background_jobs j ON j.transaction_id = t.id")
        assert (mid["t"], mid["j"]) == ("PROCESSING", "PROCESSING")
        assert mid["locked_by"] == worker.worker_id and mid["lease_token"] is not None
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
    finally:
        worker.shutdown()
    end = row(db, "SELECT processed_at, (SELECT status FROM background_jobs) AS j, "
                  "(SELECT locked_by FROM background_jobs) AS lb FROM transactions")
    assert end["processed_at"] is not None and end["j"] == "COMPLETED" and end["lb"] is None


def test_worker_heartbeat_updates_and_goes_stale(db, settings):
    worker = TransactionWorker(settings.model_copy(update={"worker_heartbeat_interval_seconds": 0.2}))
    worker.start()
    first = scalar(db, "SELECT last_heartbeat_at FROM worker_instances")
    try:
        wait_until(lambda: scalar(db, "SELECT last_heartbeat_at FROM worker_instances") > first, timeout=3)
    finally:
        worker._heartbeat.stop()  # simulate a hung/killed worker: heartbeat stops, no deregistration
    time.sleep(0.8)
    from app.database.session import session_scope
    from app.workers.job_recovery import mark_stale_workers

    with session_scope("worker") as s:
        assert mark_stale_workers(s, 0.5) == [worker.worker_id]
    assert scalar(db, "SELECT status FROM worker_instances") == "STOPPED"
    worker._executor.shutdown()


def test_worker_logs_share_transaction_correlation_id(client, db, settings):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test", "test"))
    log = logging.getLogger("app.worker")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        payload = make_payload()
        client.post("/v1/webhooks/transactions", json=payload)
        worker = TransactionWorker(settings)
        try:
            worker.poll_once()
            wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
        finally:
            worker.shutdown()
    finally:
        log.removeHandler(handler)

    corr = str(scalar(db, "SELECT correlation_id FROM transactions"))
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    job_lines = [l for l in lines if l["event"] in ("job_processing_started", "job_completed")]
    assert {l["event"] for l in job_lines} == {"job_processing_started", "job_completed"}
    assert all(l["correlation_id"] == corr and l["transaction_id"] == payload["transaction_id"] for l in job_lines)
    assert all("worker_id" in l and "job_id" in l for l in job_lines)


def test_graceful_shutdown_releases_unfinished_job(client, db, settings):
    payload = make_payload()
    client.post("/v1/webhooks/transactions", json=payload)
    worker = TransactionWorker(settings.model_copy(update={"processing_delay_seconds": 30}))
    worker.start()
    worker.poll_once()
    wait_until(lambda: scalar(db, "SELECT status FROM background_jobs") == "PROCESSING")

    started = time.perf_counter()
    worker.shutdown()
    assert time.perf_counter() - started < 5  # did not wait out the 30s delay

    state = row(db, "SELECT t.status AS t, t.processed_at, j.status AS j, j.attempts, j.locked_by FROM transactions t "
                    "JOIN background_jobs j ON j.transaction_id = t.id")
    assert state["t"] != "PROCESSED" and state["processed_at"] is None  # never falsely completed
    assert (state["j"], state["attempts"], state["locked_by"]) == ("PENDING", 0, None)
    assert scalar(db, "SELECT status FROM worker_instances") == "STOPPED"

    # a different worker (restart) picks it up and completes it
    replacement = TransactionWorker(settings)
    try:
        replacement.poll_once()
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
    finally:
        replacement.shutdown()
