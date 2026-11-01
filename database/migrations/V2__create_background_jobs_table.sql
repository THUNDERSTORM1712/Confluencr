-- One durable background job per transaction (enforced by UNIQUE in V4).
CREATE TABLE background_jobs (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id UUID         NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
    status         VARCHAR(16)  NOT NULL DEFAULT 'PENDING',
    attempts       INTEGER      NOT NULL DEFAULT 0,
    max_attempts   INTEGER      NOT NULL DEFAULT 3,
    available_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    locked_by      UUID,
    locked_until   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);
