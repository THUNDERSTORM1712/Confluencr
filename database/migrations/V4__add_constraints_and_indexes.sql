-- transactions
ALTER TABLE transactions
    ADD CONSTRAINT uq_transactions_transaction_id UNIQUE (transaction_id),
    ADD CONSTRAINT ck_transactions_amount_positive CHECK (amount > 0),
    ADD CONSTRAINT ck_transactions_status CHECK (status IN ('PROCESSING', 'PROCESSED', 'FAILED')),
    ADD CONSTRAINT ck_transactions_currency CHECK (currency ~ '^[A-Z]{3}$'),
    ADD CONSTRAINT ck_transactions_processed_at CHECK ((status = 'PROCESSED') = (processed_at IS NOT NULL));

CREATE INDEX ix_transactions_status_created_at ON transactions (status, created_at);

-- background_jobs
ALTER TABLE background_jobs
    ADD CONSTRAINT uq_background_jobs_transaction_id UNIQUE (transaction_id),
    ADD CONSTRAINT ck_background_jobs_status CHECK (status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'FAILED')),
    ADD CONSTRAINT ck_background_jobs_attempts CHECK (attempts >= 0 AND max_attempts > 0 AND attempts <= max_attempts);

-- Partial indexes that back the claim query.
CREATE INDEX ix_background_jobs_pending ON background_jobs (available_at) WHERE status = 'PENDING';
CREATE INDEX ix_background_jobs_leases ON background_jobs (locked_until) WHERE status = 'PROCESSING';

-- worker_instances
ALTER TABLE worker_instances
    ADD CONSTRAINT ck_worker_instances_status CHECK (status IN ('RUNNING', 'STOPPED'));

CREATE INDEX ix_worker_instances_heartbeat ON worker_instances (status, last_heartbeat_at);
