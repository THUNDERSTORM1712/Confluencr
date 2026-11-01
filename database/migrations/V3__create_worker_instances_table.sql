-- Worker liveness registry. Informational only: job leases remain authoritative.
CREATE TABLE worker_instances (
    worker_id         UUID         PRIMARY KEY,
    hostname          VARCHAR(255) NOT NULL,
    status            VARCHAR(16)  NOT NULL DEFAULT 'RUNNING',
    started_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_heartbeat_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    stopped_at        TIMESTAMPTZ
);
