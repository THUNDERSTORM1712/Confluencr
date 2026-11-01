-- Correlation id shared by API logs, job rows and worker logs.
ALTER TABLE transactions ADD COLUMN correlation_id UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE transactions ALTER COLUMN correlation_id DROP DEFAULT;

-- Lease fencing and error tracking.
ALTER TABLE background_jobs
    ADD COLUMN lease_token       UUID,
    ADD COLUMN last_heartbeat_at TIMESTAMPTZ,
    ADD COLUMN last_error        TEXT;

ALTER TABLE background_jobs
    ADD CONSTRAINT ck_background_jobs_lease CHECK (
        status <> 'PROCESSING'
        OR (lease_token IS NOT NULL AND locked_by IS NOT NULL AND locked_until IS NOT NULL)
    );
