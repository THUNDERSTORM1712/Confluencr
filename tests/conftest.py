"""Test fixtures. Integration tests run against a real PostgreSQL (never SQLite).

Start one with:  docker compose --profile local-db up -d postgres   (port 55432, db webhooks)
or point TEST_DATABASE_URL at any *dedicated test database* (its name must contain "test").
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:55432/webhooks_test"
)
_db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
if "test" not in _db_name:  # guard against ever running destructive tests on a real database
    raise RuntimeError(f"Refusing to run tests against database '{_db_name}': name must contain 'test'")

os.environ.update(
    {
        "ENV_FILE": "tests/.no-such-env-file",  # never read the developer's .env (it may point at Supabase)
        "RUN_WORKER_IN_API": "false",
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "DATABASE_SSL_MODE": "disable",
        "PROCESSING_DELAY_SECONDS": "1",
        "WORKER_POLL_INTERVAL_SECONDS": "0.2",
        "WORKER_LEASE_SECONDS": "2",
        "WORKER_HEARTBEAT_INTERVAL_SECONDS": "0.5",
        "RETRY_BACKOFF_BASE_SECONDS": "0.3",
        "WEBHOOK_SIGNATURE_ENABLED": "false",
        "LOG_LEVEL": "INFO",
    }
)

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.database.connection import dispose_engines, get_engine  # noqa: E402

# Hard guard: the application under test must be talking to the test database and nothing else.
assert get_settings().database_url == TEST_DATABASE_URL and not get_settings().run_worker_in_api

MIGRATIONS_DIR =Path(__file__).resolve().parent.parent / "database" / "migrations"


def _psycopg_dsn() -> str:
    return TEST_DATABASE_URL


def _ensure_schema() -> None:
    """Ensure Flyway's migrations are applied. Prefer an existing Flyway-managed schema; otherwise
    apply the same versioned SQL files in order (CI/docker-compose runs real Flyway - see test_migrations)."""
    with psycopg.connect(_psycopg_dsn(), autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('public.flyway_schema_history')").fetchone()[0]
        if exists:
            return
        for path in sorted(MIGRATIONS_DIR.glob("V*.sql"), key=lambda p: int(re.match(r"V(\d+)__", p.name).group(1))):
            conn.execute(path.read_text())
        conn.execute(
            "CREATE TABLE flyway_schema_history (installed_rank int, version varchar(50), "
            "description varchar(200), type varchar(20), script varchar(1000), checksum int, "
            "installed_by varchar(100), installed_on timestamp DEFAULT now(), execution_time int, success boolean)"
        )
        for i in range(1, 6):
            conn.execute(
                "INSERT INTO flyway_schema_history (installed_rank, version, description, type, script, "
                "installed_by, execution_time, success) VALUES (%s, %s, 'test', 'SQL', 'test', 'pytest', 0, true)",
                (i, str(i)),
            )


@pytest.fixture(scope="session", autouse=True)
def _schema():
    _ensure_schema()
    yield
    dispose_engines()


@pytest.fixture(autouse=True)
def _clean_db(_schema):
    with get_engine("api").begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS fail_job_insert ON background_jobs"))
        conn.execute(text("TRUNCATE transactions, background_jobs, worker_instances CASCADE"))
    yield


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    return get_engine("api")


def make_payload(**overrides) -> dict:
    base = {
        "transaction_id": f"txn_{uuid.uuid4().hex[:12]}",
        "source_account": "acc_user_789",
        "destination_account": "acc_merchant_456",
        "amount": 1500,
        "currency": "INR",
    }
    return {**base, **overrides}


@pytest.fixture
def payload() -> dict:
    return make_payload()


def wait_until(predicate, timeout: float = 10.0, interval: float = 0.1):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def scalar(db, sql: str, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def row(db, sql: str, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).mappings().first()
