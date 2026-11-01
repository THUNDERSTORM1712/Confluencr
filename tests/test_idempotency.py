from app.services import job_service
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, scalar, wait_until


def test_duplicate_webhooks_create_single_transaction_and_job(client, db, settings):
    payload = make_payload()
    responses = [client.post("/v1/webhooks/transactions", json=payload) for _ in range(4)]

    assert [r.status_code for r in responses] == [202] * 4
    assert [r.json()["duplicate"] for r in responses] == [False, True, True, True]
    assert scalar(db, "SELECT count(*) FROM transactions") == 1
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 1


def test_only_one_processing_lifecycle(client, db, settings):
    payload = make_payload()
    runs = []

    def processor(job, abort):
        runs.append(job.transaction_id)
        return not abort.wait(0.3)

    for _ in range(3):
        client.post("/v1/webhooks/transactions", json=payload)
    worker = TransactionWorker(settings, processor=processor)
    try:
        worker.poll_once()
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED")
        for _ in range(3):  # duplicates arriving after completion must not reset or requeue it
            assert client.post("/v1/webhooks/transactions", json=payload).json()["transaction_status"] == "PROCESSED"
        assert worker.poll_once() == 0
    finally:
        worker.shutdown()

    assert runs == [payload["transaction_id"]]
    assert scalar(db, "SELECT status FROM transactions") == "PROCESSED"
    assert scalar(db, "SELECT attempts FROM background_jobs") == 1
    assert scalar(db, "SELECT status FROM background_jobs") == "COMPLETED"


def test_completed_job_is_never_claimed_again(client, db):
    from app.database.session import session_scope
    import uuid

    client.post("/v1/webhooks/transactions", json=make_payload())
    wid = uuid.uuid4()
    with session_scope("worker") as s:
        (job,) = job_service.claim_jobs(s, wid, 5, 30)
    with session_scope("worker") as s:
        assert job_service.complete_job(s, job.job_id, job.lease_token)
    with session_scope("worker") as s:
        assert job_service.claim_jobs(s, wid, 5, 30) == []
