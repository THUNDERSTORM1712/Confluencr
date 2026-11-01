-- Transactions accepted from payment-provider webhooks.
-- Constraints and secondary indexes are added in V4; correlation_id in V5.
CREATE TABLE transactions (
    id                  UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id      VARCHAR(128)  NOT NULL,
    source_account      VARCHAR(128)  NOT NULL,
    destination_account VARCHAR(128)  NOT NULL,
    amount              NUMERIC(18,2) NOT NULL,
    currency            VARCHAR(3)    NOT NULL,
    status              VARCHAR(16)   NOT NULL DEFAULT 'PROCESSING',
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ
);
