import asyncio
import time
import uuid

import httpx

from app.database.session import session_scope
from app.main import app
from app.services import job_service
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, scalar, wait_until


async def _burst(payloads):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        return await asyncio.gather(*(ac.post("/v1/webhooks/transactions", json=p) for p in payloads))


def test_concurrent_identical_webhooks_are_atomic(db):
    payload = make_payload()
    responses = asyncio.run(_burst([payload] * 30))

    assert {r.status_code for r in responses} == {202}
    assert sum(1 for r in responses if r.json()["duplicate"] is False) == 1
    assert scalar(db, "SELECT count(*) FROM transactions") == 1
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 1


def test_multiple_unique_transactions_independent(db):
    payloads = [make_payload() for _ in range(15)]
    responses = asyncio.run(_burst(payloads))
    assert {r.status_code for r in responses} == {202}
    assert scalar(db, "SELECT count(*) FROM transactions") == 15
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 15


def test_competing_workers_never_claim_same_job(client, db):
    for _ in range(20):
        client.post("/v1/webhooks/transactions", json=make_payload())

    claimed: list[list[uuid.UUID]] = []

    def claim(worker_id):
        with session_scope("worker") as s:
            return [j.job_id for j in job_service.claim_jobs(s, worker_id, 20, 30)]

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        claimed = list(pool.map(claim, [uuid.uuid4() for _ in range(8)]))
    flat = [j for group in claimed for j in group]
    assert len(flat) == 20 and len(set(flat)) == 20


def test_webhook_latency_unaffected_by_processing_load(client, db, settings):
    slow = settings.model_copy(update={"processing_delay_seconds": 3, "worker_concurrency": 10})
    for _ in range(10):
        client.post("/v1/webhooks/transactions", json=make_payload())
    worker = TransactionWorker(slow)
    try:
        assert worker.poll_once() == 10
        wait_until(lambda: scalar(db, "SELECT count(*) FROM background_jobs WHERE status = 'PROCESSING'") == 10)
        latencies = []
        for _ in range(5):
            started = time.perf_counter()
            assert client.post("/v1/webhooks/transactions", json=make_payload()).status_code == 202
            latencies.append(time.perf_counter() - started)
        assert max(latencies) < 0.5
    finally:
        worker.shutdown()
