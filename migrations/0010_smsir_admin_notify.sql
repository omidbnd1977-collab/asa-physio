-- 0010_smsir_admin_notify.sql — forward only.
-- Clinic alerts move from Telegram to SMS.ir (provider smsir).
-- Admin appointment SMS needs kind admin_appointment on sms_messages.
-- Existing telegram delivery rows are kept; CHECK allows both for history.

CREATE TABLE deliveries_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id   INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    provider     TEXT    NOT NULL CHECK (provider IN ('file', 'webhook', 'telegram', 'smsir')),
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0 AND attempts <= 20),
    last_error   TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 500),
    delivered_at TEXT    NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (booking_id, provider)
);
INSERT INTO deliveries_new
SELECT id, booking_id, provider, status, attempts, last_error, delivered_at, created_at, updated_at
FROM deliveries;
DROP TABLE deliveries;
ALTER TABLE deliveries_new RENAME TO deliveries;
CREATE INDEX idx_deliveries_status ON deliveries(status, created_at);

CREATE TABLE sms_messages_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NULL REFERENCES patients(id) ON DELETE SET NULL,
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    phone          TEXT    NOT NULL
                           CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'registered', 'booked', 'rescheduled', 'cancelled',
                       'followup', 'confirm_request', 'custom',
                       'welcome_back', 'session_logged', 'admin_appointment')),
    body           TEXT    NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    status         TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'failed')),
    provider       TEXT    NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
    last_error     TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 400),
    sent_at        TEXT    NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO sms_messages_new
SELECT id, patient_id, appointment_id, phone, kind, body, status, provider,
       attempts, last_error, sent_at, created_at FROM sms_messages;
DROP TABLE sms_messages;
ALTER TABLE sms_messages_new RENAME TO sms_messages;
CREATE INDEX idx_sms_patient ON sms_messages(patient_id, created_at DESC);
CREATE INDEX idx_sms_status  ON sms_messages(status, created_at);
