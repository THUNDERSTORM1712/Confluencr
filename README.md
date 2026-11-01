# Transaction Webhook Service

A small, reliable backend that **receives payment webhooks, answers immediately, and processes each transaction in the background** — built with FastAPI, PostgreSQL (Supabase) and a background worker.

It was built for the *Full Stack Engineer – Backend* assessment, and is designed so that a transaction is **never lost, never processed twice, and never makes the sender wait**.

> **Try it in your browser:** open **`/ui`** — send a webhook, then watch it move from `PROCESSING` to `PROCESSED` with a live progress bar.

## 🌐 Live deployment

| | |
|---|---|
| **Public API** | https://transaction-webhook-api-z6c9.onrender.com |
| **Web UI** (easiest way to try it) | https://transaction-webhook-api-z6c9.onrender.com/ui |
| **Health check** `GET /` | https://transaction-webhook-api-z6c9.onrender.com/ |
| **Readiness** `GET /health/ready` | https://transaction-webhook-api-z6c9.onrender.com/health/ready |
| **Webhook** `POST /v1/webhooks/transactions` | https://transaction-webhook-api-z6c9.onrender.com/v1/webhooks/transactions |
| **Transaction status** `GET /v1/transactions/{id}` | https://transaction-webhook-api-z6c9.onrender.com/v1/transactions/txn_abc123def456 |

Hosted on Render (free web service, Singapore) with a Supabase PostgreSQL database (Mumbai). The API and the background worker run in one process here (`RUN_WORKER_IN_API=true`), because Render has no free background-worker plan.

**Test it from your terminal** (the status shows `PROCESSING`, then `PROCESSED` after about 30 seconds):

```bash
curl -i -X POST https://transaction-webhook-api-z6c9.onrender.com/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"txn_demo_001","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}'

curl https://transaction-webhook-api-z6c9.onrender.com/v1/transactions/txn_demo_001
```

Or run the full automated check (~35 seconds): `python scripts/smoke_test.py https://transaction-webhook-api-z6c9.onrender.com`

> **If the very first request is slow:** free Render services go to sleep after ~15 minutes without traffic and need up to ~50 seconds to wake up. A keep-awake monitor pings the service every 5 minutes to prevent this — see [Keeping the free service awake](#keeping-the-free-service-awake).

---

## Contents

1. [What it does](#what-it-does)
2. [Quick start (local)](#quick-start-local)
3. [Using it](#using-it) — web UI, API docs, curl
4. [How it works](#how-it-works)
5. [API reference](#api-reference)
6. [Configuration](#configuration)
7. [Project layout](#project-layout)
8. [Database](#database)
9. [Reliability in detail](#reliability-in-detail)
10. [Security](#security)
11. [Logging](#logging)
12. [Testing](#testing)
13. [Deploying (Supabase + Render)](#deploying-supabase--render)
14. [Troubleshooting](#troubleshooting) · [Keeping the free service awake](#keeping-the-free-service-awake)
15. [Design decisions](#design-decisions)
16. [Limitations and future work](#limitations-and-future-work)

---

## What it does

A payment provider (like Razorpay) sends your server a webhook whenever a transaction happens. It expects a **fast reply**, and it will **retry** if it doesn't get one. This service handles that safely:

| Step | What happens |
|---|---|
| 1. **Receive** | `POST /v1/webhooks/transactions` validates the data. |
| 2. **Save** | The transaction *and* its background job are written to the database in one atomic step. |
| 3. **Reply** | Only after the data is safely stored, the service answers **`202 Accepted`** (typically a few tens of milliseconds). |
| 4. **Process** | A background worker picks the job up and does 30 seconds of simulated work (standing in for external API calls). |
| 5. **Finish** | The transaction becomes `PROCESSED` with a `processed_at` time. Look it up any time with `GET /v1/transactions/{id}`. |

If the same webhook arrives again (providers retry!), the service says "got it already" and **does nothing twice**.

### Assessment checklist

| Requirement | Where it's met |
|---|---|
| `POST /v1/webhooks/transactions` → `202`, under 500 ms | [`webhooks.py`](app/api/routes/webhooks.py) — a single database write, never waits for the worker |
| `GET /` health check | [`health.py`](app/api/routes/health.py) |
| `GET /v1/transactions/{id}` | [`transactions.py`](app/api/routes/transactions.py) (returns a one-element array, exactly as the PDF shows) |
| 30-second background processing, result stored | [`transaction_worker.py`](app/workers/transaction_worker.py) |
| Same `transaction_id` → processed once | `UNIQUE` constraint + `INSERT … ON CONFLICT DO NOTHING` |
| Reliability — no lost transactions | Atomic save, job leases, retries, crash recovery |
| Public GitHub repo, README, cloud deployment | This repo · [Deploying](#deploying-supabase--render) |

Extras beyond the brief: liveness/readiness probes, optional webhook signature verification, request/correlation IDs, retries with backoff, a terminal `FAILED` status, and the web UI.

---

## Quick start (local)

**You need:** Python 3.12+ and a PostgreSQL database. The easiest database is a Supabase project (free) — see [Supabase setup](#1-set-up-supabase). For fully offline work, use the Docker one-liner below instead.

### 1. Install

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
```

### 2. Create the database tables

Tables are created by [Flyway](https://flywaydb.org) migrations (the only way the schema ever changes). With Docker installed:

```bash
docker run --rm -v "$PWD/database/migrations:/flyway/sql:ro" flyway/flyway:10 \
  -url="jdbc:postgresql://<host>:5432/<db>?sslmode=require" -user="<user>" -password="<password>" \
  -ignoreMigrationPatterns="*:pending" validate migrate info
```

You should see `now at version v5`. *(Windows PowerShell: replace `$PWD` with `${PWD}` and `\` line breaks with backticks, or put it on one line.)*

<details>
<summary><b>No Supabase yet? Use a throwaway local database</b></summary>

```bash
docker compose --profile local-db up -d postgres        # PostgreSQL on 127.0.0.1:55432, database "webhooks_test"
docker run --rm -v "$PWD/database/migrations:/flyway/sql:ro" flyway/flyway:10 \
  -url=jdbc:postgresql://host.docker.internal:55432/webhooks_test -user=postgres -password=postgres \
  -ignoreMigrationPatterns="*:pending" validate migrate info
```
Then use `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/webhooks_test` and `DATABASE_SSL_MODE=disable`.
</details>

### 3. Configure

Copy [`.env.example`](.env.example) to `.env` (it is git-ignored) and set at least:

```ini
DATABASE_URL=postgresql://postgres.<ref>:<password>@<host>.pooler.supabase.com:5432/postgres
DATABASE_SSL_MODE=require
RUN_WORKER_IN_API=true        # one process does everything - simplest for local use and the free Render plan
```

### 4. Run

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/ui**. 🎉

> **Windows tip:** use `127.0.0.1`, not `localhost`, in database URLs and when calling the API — `localhost` can add ~2 s per new connection because of an IPv6 fallback.

---

## Using it

### The web UI — `/ui`

A friendly page for trying everything without writing any code:

- **Send a webhook** with a pre-filled form (amount typos are caught before sending).
- **Send again** to see duplicate protection in action.
- **Track transactions** with a live progress bar and status badges (`PROCESSING` → `PROCESSED`), updated every 2 seconds.
- Plain-English messages for every error, with the technical details one click away.
- Works on phones, supports dark mode, and needs no internet connection or extra downloads.

Turn it off with `ENABLE_UI=false`.

### The API docs — `/docs`

Interactive Swagger page: expand an endpoint → **Try it out** → **Execute**. The webhook endpoint comes with an editable example body. (Docs are disabled when `APP_ENV=production`.)

### curl

```bash
# Send a webhook  →  202
curl -i -X POST http://127.0.0.1:8000/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"txn_abc123def456","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}'

# Check it (PROCESSING now, PROCESSED after ~30 s)
curl http://127.0.0.1:8000/v1/transactions/txn_abc123def456
```

Send the first command twice: the second reply says `"duplicate": true` and nothing is processed again.

### Automated end-to-end check

```bash
python scripts/smoke_test.py http://127.0.0.1:8000        # or your deployed URL; takes ~35 s
```
Checks health, the 202 and its speed, duplicates, the 30-second processing, and the 404 — and prints `ALL PASSED`.

---

## How it works

```mermaid
sequenceDiagram
    autonumber
    participant P as Payment provider
    participant A as API (FastAPI)
    participant D as PostgreSQL (Supabase)
    participant W as Background worker

    P->>A: POST /v1/webhooks/transactions
    A->>A: validate (+ optional signature check)
    A->>D: ONE statement: insert transaction + job (atomic, idempotent)
    D-->>A: committed
    A-->>P: 202 Accepted  (no waiting for the worker)

    loop every 2 seconds
        W->>D: claim a job (FOR UPDATE SKIP LOCKED) + take a 90 s lease
    end
    W->>W: 30 s simulated work (lease renewed every 15 s)
    W->>D: mark job COMPLETED + transaction PROCESSED (only if lease still mine)
    P->>A: GET /v1/transactions/{id}  →  PROCESSED
```

The database **is** the queue. There's no Redis, Celery or in-memory list, so nothing is lost if a process restarts. In production the API and worker can run as two separate services sharing only the database; on a free plan they can share one process (`RUN_WORKER_IN_API=true`).

### Guarantees

| Guarantee | How |
|---|---|
| **No false acknowledgement** | `202` is sent only after the database write has completed. If the database is down you get `503` and the provider retries. |
| **No half-saved transaction** | Transaction and job are inserted by a single SQL statement — both or neither. |
| **No duplicate processing** | `transaction_id` is `UNIQUE`; duplicates hit `ON CONFLICT DO NOTHING`. 30 simultaneous identical requests create exactly one transaction. |
| **No double work between workers** | Jobs are claimed with `FOR UPDATE SKIP LOCKED`; a claim holds no lock during the 30 seconds. |
| **Crash safe** | A dead worker stops renewing its lease; after it expires, another worker takes over automatically. |
| **Money is exact** | `Decimal` in Python and `NUMERIC(18,2)` in PostgreSQL — never floats. |

---

## API reference

| Method & path | Purpose | Responses |
|---|---|---|
| `POST /v1/webhooks/transactions` | Receive a webhook | `202` accepted · `400` not valid JSON · `401` bad signature · `413` too large · `422` invalid field · `503` database unavailable |
| `GET /v1/transactions/{transaction_id}` | Processing status | `200` · `404` |
| `GET /` | Health check (`{"status":"HEALTHY","current_time":…}`) | `200` |
| `GET /health/live` | Liveness — process is up, no database call | `200` |
| `GET /health/ready` | Readiness — config OK, database reachable, schema up to date | `200` · `503` |
| `GET /ui` · `GET /docs` | Web UI · Swagger docs | — |

**Webhook body**

```json
{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500,
  "currency": "INR"
}
```

| Field | Rules |
|---|---|
| `transaction_id` | 1–128 characters: letters, digits, `_ - . :` — used exactly as sent (never trimmed) |
| `source_account`, `destination_account` | 1–128 printable characters |
| `amount` | greater than 0, at most 2 decimal places, up to 18 digits |
| `currency` | a valid ISO 4217 code (`inr` is accepted and stored as `INR`) |

**Responses**

```jsonc
// 202 — new
{ "status": "accepted", "transaction_id": "txn_abc123def456", "transaction_status": "PROCESSING", "duplicate": false }
// 202 — already seen (nothing new created or reset)
{ "status": "accepted", "transaction_id": "txn_abc123def456", "transaction_status": "PROCESSED", "duplicate": true }
// GET /v1/transactions/{id}  (a one-element array, as in the PDF)
[{ "transaction_id": "txn_abc123def456", "source_account": "acc_user_789", "destination_account": "acc_merchant_456",
   "amount": 1500, "currency": "INR", "status": "PROCESSED",
   "created_at": "2024-01-15T10:30:00Z", "processed_at": "2024-01-15T10:30:30Z" }]
// Errors always look like:
{ "error": { "code": "validation_error", "message": "Invalid webhook payload", "details": [ … ] } }
```

**Statuses:** `PROCESSING` → `PROCESSED`. `FAILED` is an *additional* terminal status that appears only if processing failed on every retry.

Every response carries an `X-Request-ID` header (yours if you sent a valid one, otherwise generated).

---

## Configuration

Everything is set with environment variables (or a `.env` file). The app **refuses to start** with an invalid configuration. Full list with defaults:

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | *(required)* | PostgreSQL connection string |
| `DATABASE_SSL_MODE` | `require` | `disable` only for a local database |
| `APP_ENV` | `development` | `production` disables `/docs` and enforces safety checks |
| `RUN_WORKER_IN_API` | `false` | Run the worker inside the API process (free plan / local) |
| `ENABLE_UI` | `true` | Serve `/ui` |
| `PROCESSING_DELAY_SECONDS` | `30` | The simulated work (must be ≥ 30 in production) |
| `WORKER_POLL_INTERVAL_SECONDS` | `2` | How often the worker looks for jobs |
| `WORKER_LEASE_SECONDS` | `90` | How long a claimed job is reserved |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `15` | Lease renewal and worker heartbeat period |
| `WORKER_CONCURRENCY` | `10` | Jobs one worker runs at once |
| `WORKER_STALE_AFTER_SECONDS` | `60` | A silent worker is reported as lost after this |
| `MAX_JOB_ATTEMPTS` | `3` | Tries before a job is marked `FAILED` |
| `RETRY_BACKOFF_BASE_SECONDS` / `_MAX_` | `5` / `300` | Retry delay: 5 s, 10 s, 20 s … capped |
| `API_POOL_SIZE` / `API_MAX_OVERFLOW` | `5` / `5` | API database connections |
| `WORKER_POOL_SIZE` / `WORKER_MAX_OVERFLOW` | `3` / `2` | Worker database connections |
| `API_POOL_TIMEOUT_SECONDS` | `5` | Wait for a free connection before a `503` |
| `POOL_RECYCLE_SECONDS` | `240` | Replace connections older than this |
| `MAX_REQUEST_BYTES` | `65536` | Largest accepted webhook body |
| `WEBHOOK_SIGNATURE_ENABLED` | `false` | Verify signatures (see [Security](#security)) |
| `WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION` | `true` | Refuse to start in production without signatures |
| `WEBHOOK_SIGNATURE_PROVIDER` | `generic` | `generic` or `razorpay` |
| `WEBHOOK_SIGNING_SECRET` | – | The shared secret |
| `WEBHOOK_SIGNATURE_HEADER` / `WEBHOOK_TIMESTAMP_HEADER` | `X-Webhook-Signature` / `X-Webhook-Timestamp` | Header names (`generic`) |
| `WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | `300` | Reject older/newer signed requests |
| `LOG_LEVEL` / `LOG_FORMAT` | `INFO` / `json` | `LOG_FORMAT=text` for readable local logs |

---

## Project layout

```
app/
  main.py                    app factory, lifespan (startup/shutdown), error handlers
  config.py                  validated settings
  logging_config.py          structured JSON logging
  api/routes/                health · webhooks · transactions · ui
  static/index.html          the web UI (single file, no dependencies)
  middleware/correlation.py  X-Request-ID handling + request log line
  services/
    idempotency_service.py   the atomic "insert transaction + job" statement
    transaction_service.py   accept / look up transactions
    job_service.py           queue SQL: claim, renew, complete, fail, release
    signature_service.py     pluggable webhook signature verifiers
  workers/
    transaction_worker.py    the worker (run it with python -m app.workers.transaction_worker)
    heartbeat.py             worker registry + heartbeat
    job_recovery.py          fail jobs that ran out of attempts, flag lost workers
  models/ schemas/ database/ core/
database/migrations/         V1…V5 Flyway SQL — the only way the schema changes
tests/                       122 tests
scripts/smoke_test.py        end-to-end check for any URL
render.yaml                  Render: API + separate worker (paid worker)
render.free.yaml             Render: one free service, worker inside
.github/workflows/deploy.yml tests → flyway validate → migrate → deploy → smoke test
```

---

## Database

Three tables, managed only by Flyway migrations `V1`–`V5`:

- **`transactions`** — one row per transaction. `transaction_id` is `UNIQUE`; `amount NUMERIC(18,2)` with `CHECK (amount > 0)`; `status` is `PROCESSING | PROCESSED | FAILED`; a `correlation_id` ties API and worker logs together; all times are `TIMESTAMPTZ` (UTC).
- **`background_jobs`** — exactly one per transaction (`UNIQUE`). Tracks `status` (`PENDING | PROCESSING | COMPLETED | FAILED`), `attempts` / `max_attempts`, `available_at` (for retry backoff), and the lease: `locked_by`, `locked_until`, `lease_token`. A `CHECK` guarantees a `PROCESSING` job always has an owner, token and expiry.
- **`worker_instances`** — which workers exist and when they last sent a heartbeat (informational only; it never decides job ownership).

**Flyway rules:** never edit a migration that has been applied (a checksum check will fail and a test proves it) — add `V6__…` instead. Each migration runs in a transaction, so a failed one leaves nothing half-applied. Flyway Community has no automatic undo: roll back with a new forward migration or a Supabase backup. Use `flyway repair` only deliberately.

---

## Reliability in detail

**Claiming a job.** One SQL statement picks the next eligible job with `FOR UPDATE SKIP LOCKED`, sets it to `PROCESSING`, and gives it a fresh random `lease_token` and a 90-second lease. It commits *before* the 30-second work starts, so no database lock is held while waiting.

**The lease token is the safety net.** Renewing, completing, failing and releasing a job are all conditional on `lease_token = <mine>`. If a worker stalls and another takes the job over, the old worker's late "I'm done" updates **change zero rows** and are logged as `job_lease_lost` — it can never overwrite the new owner.

**Crash recovery.** A dead worker stops renewing. When `locked_until` passes, the same claim query hands the job to another worker — no manual database edits. A delayed *worker heartbeat* alone never causes a takeover; only an expired *lease* does.

**Retries.** If processing raises an error, the job goes back to `PENDING` with exponential backoff (5 s, 10 s, 20 s …) and the error is stored in `last_error`. After `MAX_JOB_ATTEMPTS` it becomes `FAILED` and so does the transaction. A failed job is never marked `PROCESSED`.

**Graceful shutdown.** On `SIGTERM`/`Ctrl+C` the worker stops claiming, hands unfinished jobs straight back to the queue (without using up an attempt), marks itself `STOPPED` and closes its connections. Unfinished work is never marked complete.

**Stale connections.** Managed databases drop idle connections. API calls retry once on a fresh connection if that happens, and the worker checks a connection before using it.

**Honest limit:** a database lease can't give *exactly-once* execution of an external side effect (a worker could crash after calling an external API but before saving the result). When you replace the simulated work with a real provider call, pass an idempotency key derived from `transaction_id`.

---

## Security

- **Optional signature verification** (`WEBHOOK_SIGNATURE_ENABLED=true`) over the *raw* request body with a constant-time comparison, a timestamp freshness window, and replay protection (a replay of an accepted webhook is simply a harmless duplicate). Providers are pluggable in [`signature_service.py`](app/services/signature_service.py):
  - `generic` — HMAC-SHA256 of `"{timestamp}.{body}"` in `X-Webhook-Signature` + `X-Webhook-Timestamp`.
  - `razorpay` — HMAC-SHA256 of the body in `X-Razorpay-Signature`.

  Match your real provider's exact scheme before relying on it.
- **Production guard:** with `APP_ENV=production` the app refuses to start unless signatures are enabled (override: `WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION=false`).
- **Input is untrusted:** strict validation, control characters rejected, request size capped, parameterised SQL only, exact decimal parsing.
- **Safe errors:** clients get short messages — never stack traces, SQL, or connection details. Secrets and signatures are never logged.
- **The UI** renders all text with `textContent` (no HTML injection) and loads nothing from third parties.
- Docs endpoints are off in production. Secrets live in environment variables only; `.env` is git-ignored.

> ⚠️ **Assessment note:** the grader sends *unsigned* webhooks, so the Render blueprints explicitly set `WEBHOOK_SIGNATURE_ENABLED=false` and `WEBHOOK_SIGNATURE_REQUIRED_IN_PRODUCTION=false`. Accepting unsigned webhooks from the public internet is **not** production-safe — for a real provider, turn signatures on and set `WEBHOOK_SIGNING_SECRET`.

---

## Logging

JSON, one object per line, to stdout (Render captures it). Typical fields: `timestamp`, `log_level`, `service_name`, `event`, `request_id`, `correlation_id`, `transaction_id`, `worker_id`, `job_id`, `processing_status`, `duration_ms`, `error_type`.

- `request_id` comes from the `X-Request-ID` header (if valid) or is generated, and is returned on every response.
- `correlation_id` is created when a transaction is first accepted and stored on its row, so **the API and the worker log the same ID** for one transaction — search for it to see the whole story.
- Useful events: `webhook_accepted`, `webhook_duplicate_ignored`, `job_processing_started`, `job_completed`, `job_retry_scheduled`, `job_failed_permanently`, `job_lease_lost`, `worker_heartbeat_lost`.

Set `LOG_FORMAT=text` for readable local output.

---

## Testing

```bash
docker compose --profile local-db up -d postgres      # test database on 127.0.0.1:55432
python -m pytest -q tests                              # ~90 seconds
```

**122 tests**, run against a real PostgreSQL (never SQLite), covering:

- **Basics:** health/liveness/readiness (including database down and outdated schema), validation, the 202 and its speed.
- **Atomicity:** an injected failure while saving the job leaves *nothing* stored and returns 503, never a false 202.
- **Idempotency:** repeated duplicates, 30 simultaneous identical requests, duplicate after completion, different payload with the same ID.
- **Worker:** `PROCESSING` → `PROCESSED`, competing workers, 60 jobs across 2 workers each processed exactly once, lease renewal on long jobs, graceful shutdown, API/worker restarts.
- **Failure handling:** crashed-worker recovery via lease expiry, stale lease tokens rejected, retries with backoff, terminal failure, database briefly down at worker start, dropped connections.
- **Hostile input:** NaN/huge/exponent amounts, control characters, NUL bytes, deeply nested JSON, invalid UTF-8, oversized chunked bodies, SQL injection text.
- **Security:** valid/invalid/stale/replayed signatures, Razorpay mode, no secrets in logs, UI free of external resources.
- **Correlation IDs** across API and worker logs.
- **Migrations:** real Flyway applies the SQL, validates, and detects a tampered migration (needs Docker; skipped otherwise).

Tests use a 1-second delay (`PROCESSING_DELAY_SECONDS=1`) to stay fast; production keeps 30, and a test checks that default and the production guard. Tests **ignore your `.env`** and refuse to run against any database whose name doesn't contain `test`, so they can never touch real data.

---

## Deploying (Supabase + Render)

You need free accounts on **GitHub**, **Supabase** and **Render**. Order matters: *database → tables → service → verify*.

### 1. Set up Supabase

1. Create a project. Pick the region **closest to where Render will run** (India → *Mumbai* or *Singapore*; Render's nearest region to India is **Singapore**). Save the database password.
2. Click **Connect** and copy the **Session pooler** string (port **5432**). It looks like  
   `postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`  
   This is your `DATABASE_URL`. (The *Transaction pooler*, port 6543, also works for the app, but **never** use it for Flyway.)

### 2. Create the tables (once, and again after any new migration)

Run the Flyway command from [Quick start](#2-create-the-database-tables) with your Supabase details:

- URL `jdbc:postgresql://aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`
- user `postgres.<project-ref>`, and your password.

If Flyway reports *"non-empty schema … no schema history table"*, Supabase's `public` schema already holds one of its own helper objects. Check it contains no tables of yours, then add `-baselineOnMigrate=true -baselineVersion=0`.

### 3. Deploy to Render

**Free plan — one service, worker inside** (Render has no free background workers):

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → pick the repo → set the blueprint file to **`render.free.yaml`**.
3. Set the secret `DATABASE_URL`. Choose the **Singapore** region.
4. Deploy. The health check is `/health/ready`.

**Paid — API and worker as separate services:** use `render.yaml` instead (the worker needs a paid plan) and set `DATABASE_URL` on **both** services.

> 💤 **Free-plan sleep:** free web services sleep after ~15 minutes idle and take 30–50 s to wake; nothing is processed while asleep (jobs wait safely in the database). Set up a keep-awake monitor — see [below](#keeping-the-free-service-awake) — **before sharing the URL**.

### Keeping the free service awake

A sleeping service would answer the first request slowly (up to ~50 s), which breaks the "respond within 500 ms" requirement. The fix is to send it a tiny request every 5 minutes so it never goes idle. `GET /health/live` is ideal: it doesn't touch the database.

**Option A — UptimeRobot (recommended, free, ~2 minutes):**

1. Sign up at https://uptimerobot.com and click **+ New monitor** (or *Add New Monitor*).
2. **Monitor type:** `HTTP(s)`.
3. **Friendly name:** `Transaction Webhook API`.
4. **URL:** `https://transaction-webhook-api-z6c9.onrender.com/health/live`
5. **Monitoring interval:** `5 minutes` (the free plan's minimum).
6. Click **Create monitor**. The status should turn green ("Up") within a minute.

**Option B — cron-job.org (also free):** create a job with the same URL, schedule *every 5 minutes*, method `GET`.

**Option C — built-in backup:** [`.github/workflows/keep-awake.yml`](.github/workflows/keep-awake.yml) pings the service every 5 minutes from GitHub Actions. GitHub may run scheduled jobs several minutes late and pauses them after 60 days without repository activity, so use it **in addition to** Option A or B, not instead.

Check it works: in the Render dashboard open the service → **Logs**; you should see a request to `/health/live` every few minutes.

### 4. Verify

```bash
curl https://<your-service>.onrender.com/health/ready
python scripts/smoke_test.py https://<your-service>.onrender.com
```

`.github/workflows/deploy.yml` automates this as: tests → `flyway validate` → `flyway migrate` → deploy via Render deploy hooks → smoke test. It needs the repository secrets `FLYWAY_URL`, `FLYWAY_USER`, `FLYWAY_PASSWORD`, `RENDER_API_DEPLOY_HOOK`, `RENDER_WORKER_DEPLOY_HOOK` (paid layout only), `PUBLIC_API_URL`. A shared concurrency group prevents two deployments from migrating at once, and the API and worker never run migrations themselves.

**Monitoring:** watch the API through `/health/ready`. In the two-service layout, watch the worker through `worker_instances.last_heartbeat_at` or `worker_heartbeat_lost` log lines — a healthy API doesn't prove a healthy worker.

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `400 malformed_request` | The body isn't valid JSON — e.g. a typo like `150l0` (a letter *l*) instead of `1500`. |
| `422 validation_error` | Valid JSON but a bad value: space in the ID, amount ≤ 0 or > 2 decimals, unknown currency, control characters. The `details` list says which field. |
| Status stays `PROCESSING` | The worker isn't running. Local: set `RUN_WORKER_IN_API=true` or start `python -m app.workers.transaction_worker`. Render free: the service may be asleep (see the keep-awake monitor). |
| `/health/ready` → 503 | Check `checks` in the response: `database` = can't connect (wrong URL/password); `schema` = migrations not applied. |
| Startup error mentioning a setting | The configuration is validated on boot; the message names the setting (e.g. signatures required in production). |
| First request after a while is slow | Free-plan wake-up, or (Windows) using `localhost` instead of `127.0.0.1`. |
| Flyway "non-empty schema" on Supabase | See [step 2](#2-create-the-tables-once-and-again-after-any-new-migration) above. |
| Tests can't connect | Start the test database: `docker compose --profile local-db up -d postgres`. |

---

## Design decisions

| Decision | Why |
|---|---|
| **PostgreSQL as the job queue** | The transaction and its job must be created *together* — impossible to guarantee across a separate broker without extra machinery. One system, one commit. Redis/Celery/RabbitMQ would add moving parts for no gain at this scale. |
| **One SQL statement for "save"** | A single `INSERT … ON CONFLICT` CTE is atomic, idempotent, and costs one network round trip — important when the database is far from the server. |
| **Leases with fencing tokens** | Lets crashed workers be recovered automatically while making it impossible for a stale worker to overwrite a newer one. |
| **Flyway for the schema** | Versioned, checksummed, auditable. No ORM `create_all()` and no Alembic. |
| **Sync SQLAlchemy in a thread pool** | Simple and plenty fast here (tens of ms per request); the heavy waiting is in the worker, not the API. |
| **`RUN_WORKER_IN_API` option** | Render has no free background worker. The same worker code runs on a thread — a trade-off, not a different design. |
| **Single-file UI, no framework** | No build step, no CDN, works offline, easy to read and review. |
| **Array response for `GET /v1/transactions/{id}`** | Matches the assessment PDF exactly. |

---

## Limitations and future work

**Known limitations**

- The 30-second "external call" is simulated; real calls need provider idempotency keys (see [Reliability](#reliability-in-detail)).
- The worker polls every 2 s, so pickup can take up to ~2 s.
- A duplicate webhook with a *different* payload keeps the original and only logs a warning.
- `FAILED` transactions need an operator to look at them — there's no replay endpoint yet.
- On the free Render plan the API and worker share one process and one 512 MB instance, and sleep when idle.
- The `generic` signature format is illustrative; configure the exact scheme of your real provider.

**Ideas for later:** `LISTEN/NOTIFY` instead of polling, a dead-letter replay endpoint, metrics and alerts (stale workers, failed jobs), a least-privilege database role for the app, rate limiting, per-provider event-ID tracking.
