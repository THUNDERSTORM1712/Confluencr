"""End-to-end smoke test against a running deployment (stdlib only).

    python scripts/smoke_test.py https://transaction-webhook-api.onrender.com
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            return res.status, json.loads(res.read() or b"null"), time.perf_counter() - started
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null"), time.perf_counter() - started


def main(base: str) -> int:
    base = base.rstrip("/")
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")

    status, body, _ = call("GET", f"{base}/")
    check("GET / healthy", status == 200 and body["status"] == "HEALTHY", str(body))
    status, body, _ = call("GET", f"{base}/health/ready")
    check("GET /health/ready", status == 200, str(body))

    txn = f"txn_smoke_{uuid.uuid4().hex[:10]}"
    payload = {"transaction_id": txn, "source_account": "acc_user_789", "destination_account": "acc_merchant_456",
               "amount": 1500, "currency": "INR"}
    status, body, elapsed = call("POST", f"{base}/v1/webhooks/transactions", payload)
    check("webhook -> 202", status == 202, f"({elapsed * 1000:.0f} ms) {body}")
    check("webhook < 500ms", elapsed < 0.5, f"({elapsed * 1000:.0f} ms)")
    sent_at = time.perf_counter()

    for _ in range(3):
        status, body, _ = call("POST", f"{base}/v1/webhooks/transactions", payload)
        check("duplicate webhook -> 202 (duplicate)", status == 202 and body["duplicate"] is True)

    status, body, _ = call("GET", f"{base}/v1/transactions/{txn}")
    check("status PROCESSING right after webhook", status == 200 and body[0]["status"] == "PROCESSING", str(body))

    final = None
    while time.perf_counter() - sent_at < 120:
        status, body, _ = call("GET", f"{base}/v1/transactions/{txn}")
        if status == 200 and body[0]["status"] == "PROCESSED":
            final = body[0]
            break
        time.sleep(1)
    waited = time.perf_counter() - sent_at
    check("transaction PROCESSED", final is not None, f"after {waited:.1f}s {final}")
    check("processing took >= 30s", final is not None and waited >= 29, f"({waited:.1f}s)")
    check("processed_at populated", final is not None and final["processed_at"])
    status, body, _ = call("GET", f"{base}/v1/transactions/txn_does_not_exist")
    check("unknown transaction -> 404", status == 404)
    print("ALL PASSED" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"))
