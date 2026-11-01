"""Adversarial / boundary tests: hostile input, odd JSON numbers, stale connections, worker resilience, volume."""
import threading
import time
import uuid

import pytest
from sqlalchemy import text

from app.core.security import hmac_sha256_hex
from app.database.session import session_scope
from app.services import job_service
from app.services.signature_service import GenericHmacVerifier
from app.workers import heartbeat
from app.workers.transaction_worker import TransactionWorker
from tests.conftest import make_payload, scalar, wait_until

URL = "/v1/webhooks/transactions"
JSON_HDR = {"content-type": "application/json"}


def raw(client, body, **kw):
    return client.post(URL, content=body, headers=JSON_HDR, **kw)


# ---------------------------------------------------------------- hostile / malformed input
def test_deeply_nested_json_is_400_not_500(client):
    assert raw(client, "[" * 50000).status_code == 400


def test_empty_body_and_whitespace_body(client):
    assert raw(client, b"").status_code == 400
    assert raw(client, b"   ").status_code == 400


def test_invalid_utf8_body(client):
    assert raw(client, b'{"transaction_id": "\xff\xfe"}').status_code == 400


def test_json_scalar_and_null_bodies(client):
    for body in (b"null", b"42", b'"text"', b"true"):
        assert raw(client, body).status_code == 400


@pytest.mark.parametrize("bad", ["a\u0000b", "a\nb", "a\tb", "a\x1bb", "‎\x07"])
def test_control_characters_in_accounts_rejected(client, db, bad):
    res = client.post(URL, json=make_payload(source_account=bad))
    assert res.status_code == 422  # never 5xx: a provider would retry forever
    assert scalar(db, "SELECT count(*) FROM transactions") == 0


@pytest.mark.parametrize("tid", ["txn\n", " txn_1", "txn_1 ", "té", "txn/1", "a" * 129, "txn\u0000", "txn;drop"])
def test_bad_transaction_ids_rejected(client, tid):
    assert client.post(URL, json=make_payload(transaction_id=tid)).status_code == 422


def test_whitespace_only_accounts_rejected(client):
    assert client.post(URL, json=make_payload(source_account="   ")).status_code == 422


def test_account_length_boundary(client):
    assert client.post(URL, json=make_payload(source_account="a" * 128)).status_code == 202
    assert client.post(URL, json=make_payload(source_account="a" * 129)).status_code == 422


def test_unicode_account_names_roundtrip(client, db):
    body = make_payload(source_account="जीवन \U0001F600", destination_account="Zoë & Co.")
    assert client.post(URL, json=body).status_code == 202
    got = client.get(f"/v1/transactions/{body['transaction_id']}").json()[0]
    assert got["source_account"] == "जीवन \U0001F600" and got["destination_account"] == "Zoë & Co."


def test_sql_injection_is_stored_literally(client, db):
    evil = "x'); DROP TABLE transactions; --"
    body = make_payload(source_account=evil)
    assert client.post(URL, json=body).status_code == 202
    assert scalar(db, "SELECT source_account FROM transactions") == evil
    assert scalar(db, "SELECT count(*) FROM transactions") == 1
    assert client.get("/v1/transactions/x'%20OR%20'1'='1").status_code == 404


def test_extra_and_unknown_fields_ignored(client):
    assert client.post(URL, json={**make_payload(), "is_admin": True, "nested": {"a": 1}}).status_code == 202


def test_oversized_chunked_body_rejected(client):
    def gen():
        for _ in range(80):
            yield b"x" * 1024  # 80 KiB > 64 KiB limit, no Content-Length

    assert client.post(URL, content=gen(), headers=JSON_HDR).status_code == 413


# ---------------------------------------------------------------- amounts
@pytest.mark.parametrize(
    "amount, ok",
    [(0.01, True), (1, True), (9999999999999999.99, False),  # float cannot hold it exactly -> see decimal test
     (0.001, False), (0, False), (-0, False), (-0.01, False), (True, False), (None, False), ("", False),
     ([], False), ({}, False), ("1e999", False)],
)
def test_amount_boundaries(client, amount, ok):
    res = client.post(URL, json=make_payload(amount=amount))
    assert (res.status_code == 202) == ok, res.text
    assert res.status_code < 500


def test_exact_decimal_extremes_via_raw_json(client, db):
    def body(amount, tid):
        return (f'{{"transaction_id":"{tid}","source_account":"a","destination_account":"b",'
                f'"amount":{amount},"currency":"INR"}}')

    assert raw(client, body("9999999999999999.99", "txn_max")).status_code == 202   # 18 digits: NUMERIC(18,2) max
    assert str(scalar(db, "SELECT amount FROM transactions WHERE transaction_id='txn_max'")) == "9999999999999999.99"
    assert raw(client, body("10000000000000000", "txn_over")).status_code == 422     # 17 integer digits
    assert raw(client, body("1e2", "txn_exp")).status_code == 202
    assert str(scalar(db, "SELECT amount FROM transactions WHERE transaction_id='txn_exp'")) == "100.00"
    assert raw(client, body("NaN", "txn_nan")).status_code == 422
    assert raw(client, body("Infinity", "txn_inf")).status_code == 422
    assert raw(client, body("-Infinity", "txn_ninf")).status_code == 422
    assert raw(client, body("150l0", "txn_typo")).status_code == 400
    assert raw(client, body("0.10", "txn_dime")).status_code == 202   # float would give 0.1000000000000000055


def test_currency_edge_cases(client):
    for ok in ("INR", "inr", " usd ", "EUR", "JPY"):
        assert client.post(URL, json=make_payload(currency=ok)).status_code == 202
    for bad in ("", "IN", "INRR", "ZZZ", "12A", "₹", None, 5):
        assert client.post(URL, json=make_payload(currency=bad)).status_code == 422


# ---------------------------------------------------------------- idempotency subtleties
def test_ids_are_case_sensitive_and_distinct(client, db):
    assert client.post(URL, json=make_payload(transaction_id="TXN_A")).json()["duplicate"] is False
    assert client.post(URL, json=make_payload(transaction_id="txn_a")).json()["duplicate"] is False
    assert scalar(db, "SELECT count(*) FROM transactions") == 2


def test_duplicate_with_different_payload_keeps_original(client, db):
    tid = "txn_conflict"
    client.post(URL, json=make_payload(transaction_id=tid, amount=100))
    res = client.post(URL, json=make_payload(transaction_id=tid, amount=999, currency="USD"))
    assert res.status_code == 202 and res.json()["duplicate"] is True
    assert str(scalar(db, "SELECT amount FROM transactions")) == "100.00"
    assert scalar(db, "SELECT currency FROM transactions") == "INR"
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 1


# ---------------------------------------------------------------- responses / routing
def test_every_response_carries_request_id(client):
    for res in (client.get("/nope"), client.get("/v1/transactions/missing"), client.post(URL, json={}),
                raw(client, b"{"), client.get("/")):
        assert uuid_ok(res.headers["x-request-id"]), res.status_code


def uuid_ok(value):
    uuid.UUID(value)
    return True


def test_routing_edge_cases(client):
    assert client.get("/nope").status_code == 404
    assert client.get(URL).status_code == 405
    assert client.delete("/v1/transactions/x").status_code == 405
    assert client.get("/v1/transactions/").status_code in (404, 307)


def test_transaction_lookup_edge_ids(client):
    assert client.get("/v1/transactions/txn%00x").status_code == 404   # NUL must not reach PostgreSQL
    assert client.get("/v1/transactions/a%20b").status_code == 404
    assert client.get("/v1/transactions/" + "a" * 129).status_code == 422
    assert client.get("/v1/transactions/" + "a" * 128).status_code == 404


def test_error_bodies_do_not_leak_internals(monkeypatch):
    import app.api.routes.webhooks as hooks
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)  # observe the 500 response instead of re-raising

    def boom(*a, **k):
        raise RuntimeError("secret internal detail postgres://user:pw@host")

    monkeypatch.setattr(hooks, "accept_transaction", boom)
    res = client.post(URL, json=make_payload())
    assert res.status_code == 500 and "postgres://" not in res.text and "secret" not in res.text


# ---------------------------------------------------------------- signature edge cases
@pytest.mark.parametrize("ts", ["nan", "inf", "-inf", "abc", "", "1e999", "0", "-1"])
def test_bad_timestamps_rejected(ts):
    v = GenericHmacVerifier("s", "X-Webhook-Signature", "X-Webhook-Timestamp", 300)
    sig = hmac_sha256_hex("s", ts.encode() + b".{}")
    from app.core.exceptions import SignatureError

    with pytest.raises(SignatureError):
        v.verify(b"{}", {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": sig})


def test_signature_prefix_and_case_variants_accepted():
    v = GenericHmacVerifier("s", "X-Webhook-Signature", "X-Webhook-Timestamp", 300)
    ts = str(int(time.time()))
    sig = hmac_sha256_hex("s", ts.encode() + b".{}")
    for variant in (sig, "sha256=" + sig, sig.upper(), f"  {sig}  "):
        v.verify(b"{}", {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": variant})
    # future timestamp within tolerance accepted, far future rejected
    far = str(int(time.time()) + 10_000)
    from app.core.exceptions import SignatureError

    with pytest.raises(SignatureError):
        v.verify(b"{}", {"X-Webhook-Timestamp": far, "X-Webhook-Signature": hmac_sha256_hex("s", far.encode() + b".{}")})


# ---------------------------------------------------------------- stale connections
def _kill_api_connections(db):
    with db.begin() as conn:
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE application_name LIKE '%-api' AND pid <> pg_backend_pid()"))


def test_requests_survive_server_side_connection_drops(client, db):
    client.post(URL, json=make_payload())  # make sure pooled connections exist
    first = make_payload()
    _kill_api_connections(db)
    assert client.post(URL, json=first).status_code == 202          # retried transparently
    _kill_api_connections(db)
    assert client.get(f"/v1/transactions/{first['transaction_id']}").status_code == 200
    _kill_api_connections(db)
    assert client.get("/health/ready").status_code == 200
    assert scalar(db, "SELECT count(*) FROM transactions WHERE transaction_id = :t", t=first["transaction_id"]) == 1


# ---------------------------------------------------------------- worker resilience
def test_worker_registration_retries_when_db_unavailable_at_start(db, settings, monkeypatch):
    real, calls = heartbeat.register_worker, {"n": 0}

    def flaky(worker_id, hostname):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("db down")
        return real(worker_id, hostname)

    monkeypatch.setattr(heartbeat, "register_worker", flaky)
    worker = TransactionWorker(settings.model_copy(update={"worker_poll_interval_seconds": 0.1}))
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    try:
        wait_until(lambda: scalar(db, "SELECT count(*) FROM worker_instances") == 1, timeout=10)
        assert calls["n"] == 3
    finally:
        worker.request_stop()
        t.join(timeout=10)
    assert scalar(db, "SELECT status FROM worker_instances") == "STOPPED"


def test_worker_loop_survives_poll_errors(client, db, settings, monkeypatch):
    client.post(URL, json=make_payload())
    real, calls = job_service.claim_jobs, {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("transient db error")
        return real(*a, **k)

    monkeypatch.setattr(job_service, "claim_jobs", flaky)
    worker = TransactionWorker(settings.model_copy(update={"worker_poll_interval_seconds": 0.1}))
    t = threading.Thread(target=worker.run, daemon=True)
    t.start()
    try:
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED", timeout=15)
    finally:
        worker.request_stop()
        t.join(timeout=10)
    assert calls["n"] >= 3


def test_lease_renewal_keeps_long_job_from_being_reclaimed(client, db, settings):
    client.post(URL, json=make_payload())

    def slow(job, abort):
        return not abort.wait(4.5)  # > 2x the 2s lease: only survives because the lease is renewed

    worker = TransactionWorker(settings, processor=slow)
    try:
        assert worker.poll_once() == 1
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with session_scope("worker") as s:
                assert job_service.claim_jobs(s, uuid.uuid4(), 5, 30) == []  # nobody can steal it
            time.sleep(0.4)
        wait_until(lambda: scalar(db, "SELECT status FROM transactions") == "PROCESSED", timeout=10)
    finally:
        worker.shutdown()
    assert scalar(db, "SELECT attempts FROM background_jobs") == 1


# ---------------------------------------------------------------- volume + competing workers
def test_many_jobs_two_workers_each_processed_exactly_once(client, db, settings):
    lock, seen = threading.Lock(), []

    def processor(job, abort):
        with lock:
            seen.append(job.transaction_id)
        return not abort.wait(0.2)

    ids = [make_payload()["transaction_id"] for _ in range(60)]
    for tid in ids:
        assert client.post(URL, json=make_payload(transaction_id=tid)).status_code == 202
    cfg = settings.model_copy(update={"worker_poll_interval_seconds": 0.1, "worker_concurrency": 8})
    workers = [TransactionWorker(cfg, processor=processor) for _ in range(2)]
    threads = [threading.Thread(target=w.run, daemon=True) for w in workers]
    for t in threads:
        t.start()
    try:
        wait_until(lambda: scalar(db, "SELECT count(*) FROM transactions WHERE status = 'PROCESSED'") == 60, timeout=60)
    finally:
        for w in workers:
            w.request_stop()
        for t in threads:
            t.join(timeout=15)

    assert sorted(seen) == sorted(ids)  # every transaction processed exactly once, none twice
    assert scalar(db, "SELECT count(*) FROM background_jobs WHERE status = 'COMPLETED' AND attempts = 1") == 60
    assert scalar(db, "SELECT count(*) FROM transactions WHERE processed_at IS NULL") == 0
    assert scalar(db, "SELECT count(DISTINCT locked_by) FROM background_jobs") == 0  # all released
