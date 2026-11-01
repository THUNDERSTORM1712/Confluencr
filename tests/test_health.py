from datetime import datetime

from app.api.routes import health
from app.database.connection import build_engine


def test_root_health(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "HEALTHY"
    datetime.strptime(body["current_time"], "%Y-%m-%dT%H:%M:%SZ")


def test_ready_ok_with_database(client):
    res = client.get("/health/ready")
    assert res.status_code == 200
    assert res.json()["checks"] == {
        "initialized": "ok", "configuration": "ok", "database": "ok", "schema": "ok",
    }


def test_liveness_independent_of_db_and_readiness_503_when_db_down(client, monkeypatch, settings):
    dead = build_engine(settings.model_copy(update={"database_url": "postgresql://x:y@127.0.0.1:1/nope"}), "api")
    monkeypatch.setattr(health, "get_engine", lambda role="api": dead)
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"]["database"] == "unavailable"
    assert "x:y" not in ready.text  # no connection details leak


def test_ready_503_when_schema_outdated(client, monkeypatch):
    monkeypatch.setattr(health, "REQUIRED_SCHEMA_VERSION", 999)
    res = client.get("/health/ready")
    assert res.status_code == 503
    assert res.json()["checks"]["schema"] == "outdated"
