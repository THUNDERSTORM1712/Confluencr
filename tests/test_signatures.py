import json
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.main as main
from app.config import Settings
from app.core.security import hmac_sha256_hex
from tests.conftest import make_payload, scalar

SECRET = "whsec_test_secret"


def _client(monkeypatch, settings, **overrides):
    cfg = settings.model_copy(update={"webhook_signature_enabled": True, "webhook_signing_secret": SECRET, **overrides})
    monkeypatch.setattr(main, "get_settings", lambda: cfg)
    return TestClient(main.create_app())


def _signed(body: bytes, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    return {"X-Webhook-Timestamp": ts, "X-Webhook-Signature": hmac_sha256_hex(SECRET, ts.encode() + b"." + body),
            "content-type": "application/json"}


def _body():
    return json.dumps(make_payload()).encode()


def test_valid_signature_accepted(monkeypatch, settings, db):
    c = _client(monkeypatch, settings)
    body = _body()
    assert c.post("/v1/webhooks/transactions", content=body, headers=_signed(body)).status_code == 202
    assert scalar(db, "SELECT count(*) FROM transactions") == 1


def test_invalid_signature_rejected_and_nothing_persisted(monkeypatch, settings, db):
    c = _client(monkeypatch, settings)
    body = _body()
    headers = _signed(body)
    headers["X-Webhook-Signature"] = "0" * 64
    res = c.post("/v1/webhooks/transactions", content=body, headers=headers)
    assert res.status_code == 401 and res.json()["error"]["code"] == "invalid_signature"
    assert scalar(db, "SELECT count(*) FROM transactions") == 0


def test_tampered_body_rejected(monkeypatch, settings):
    c = _client(monkeypatch, settings)
    body = _body()
    headers = _signed(body)
    assert c.post("/v1/webhooks/transactions", content=body.replace(b"1500", b"9999"), headers=headers).status_code == 401


def test_stale_timestamp_rejected(monkeypatch, settings, db):
    c = _client(monkeypatch, settings)
    body = _body()
    res = c.post("/v1/webhooks/transactions", content=body, headers=_signed(body, ts=int(time.time()) - 3600))
    assert res.status_code == 401 and res.json()["error"]["code"] == "stale_timestamp"
    assert scalar(db, "SELECT count(*) FROM transactions") == 0


def test_missing_headers_rejected(monkeypatch, settings):
    c = _client(monkeypatch, settings)
    assert c.post("/v1/webhooks/transactions", content=_body(), headers={"content-type": "application/json"}).status_code == 401


def test_replayed_request_is_idempotent(monkeypatch, settings, db):
    c = _client(monkeypatch, settings)
    body = _body()
    headers = _signed(body)
    assert c.post("/v1/webhooks/transactions", content=body, headers=headers).json()["duplicate"] is False
    assert c.post("/v1/webhooks/transactions", content=body, headers=headers).json()["duplicate"] is True
    assert scalar(db, "SELECT count(*) FROM background_jobs") == 1


def test_razorpay_provider(monkeypatch, settings):
    c = _client(monkeypatch, settings, webhook_signature_provider="razorpay")
    body = _body()
    good = {"X-Razorpay-Signature": hmac_sha256_hex(SECRET, body), "content-type": "application/json"}
    assert c.post("/v1/webhooks/transactions", content=body, headers=good).status_code == 202
    bad = {**good, "X-Razorpay-Signature": "deadbeef"}
    assert c.post("/v1/webhooks/transactions", content=_body(), headers=bad).status_code == 401


def test_verification_disabled_only_in_intended_configuration(client):
    assert client.post("/v1/webhooks/transactions", json=make_payload()).status_code == 202  # test env: disabled

    base = dict(database_url="postgresql://u:p@h/db", processing_delay_seconds=30)
    with pytest.raises(ValidationError):  # production + required + disabled -> refuses to start
        Settings(app_env="production", webhook_signature_enabled=False, **base)
    with pytest.raises(ValidationError):  # enabled without secret
        Settings(webhook_signature_enabled=True, webhook_signing_secret="", **base)
    Settings(app_env="production", webhook_signature_enabled=True, webhook_signing_secret="s", **base)
    Settings(app_env="production", webhook_signature_required_in_production=False, **base)  # explicit opt-out


def test_secrets_not_logged(monkeypatch, settings, capsys):
    c = _client(monkeypatch, settings)
    body = _body()
    bad = _signed(body)
    bad["X-Webhook-Signature"] = "f" * 64
    c.post("/v1/webhooks/transactions", content=body, headers=bad)
    out = capsys.readouterr().out
    assert SECRET not in out and "f" * 64 not in out
