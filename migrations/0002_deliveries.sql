-- 0002_deliveries.sql — forward only.
-- A booking is only "done" once the clinic has actually received it. This table is the
-- proof behind the success message the patient sees.

CREATE TABLE deliveries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    provider     TEXT    NOT NULL CHECK (provider IN ('file', 'webhook', 'telegram')),
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 20),
    last_error   TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 500),
    delivered_at TEXT    NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (booking_id, provider)
);
CREATE INDEX idx_deliveries_status ON deliveries(status, created_at);
