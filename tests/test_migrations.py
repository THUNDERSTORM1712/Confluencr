"""Runs the real Flyway (Docker image) against a scratch database. Skipped when Docker is unavailable."""
import shutil
import subprocess
import uuid
from urllib.parse import urlparse

import psycopg
import pytest

from tests.conftest import MIGRATIONS_DIR, TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or subprocess.run(["docker", "info"], capture_output=True).returncode != 0,
    reason="Docker is required to run Flyway",
)


@pytest.fixture
def scratch_db():
    name = f"flyway_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    u = urlparse(TEST_DATABASE_URL)
    yield name, u
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def flyway(db, migrations_dir, *commands):
    name, u = db
    host = "host.docker.internal" if u.hostname in ("localhost", "127.0.0.1") else u.hostname
    return subprocess.run(
        ["docker", "run", "--rm", "--add-host", "host.docker.internal:host-gateway",
         "-v", f"{migrations_dir}:/flyway/sql:ro", "flyway/flyway:10",
         f"-url=jdbc:postgresql://{host}:{u.port or 5432}/{name}", f"-user={u.username}", f"-password={u.password}",
         "-connectRetries=3", "-ignoreMigrationPatterns=*:pending", *commands],
        capture_output=True, text=True, timeout=180,
    )


def test_flyway_migrates_and_validates(scratch_db):
    res = flyway(scratch_db, MIGRATIONS_DIR, "validate", "migrate", "validate")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "now at version v5" in res.stdout

    name, u = scratch_db
    dsn = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{name}"
    with psycopg.connect(dsn) as conn:
        tables = {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert {"transactions", "background_jobs", "worker_instances", "flyway_schema_history"} <= tables
        (amount_type,) = conn.execute(
            "SELECT numeric_precision || ',' || numeric_scale FROM information_schema.columns "
            "WHERE table_name='transactions' AND column_name='amount'"
        ).fetchone()
        assert amount_type == "18,2"


def test_flyway_detects_checksum_mismatch(scratch_db, tmp_path):
    work = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, work)
    assert flyway(scratch_db, work, "migrate").returncode == 0

    (work / "V1__create_transactions_table.sql").write_text(
        (work / "V1__create_transactions_table.sql").read_text() + "\n-- tampered after being applied\n"
    )
    res = flyway(scratch_db, work, "validate")
    assert res.returncode != 0
    assert "checksum mismatch" in (res.stdout + res.stderr).lower()
