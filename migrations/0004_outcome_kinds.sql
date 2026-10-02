-- 0004_outcome_kinds.sql — forward only.
-- The portal adds a real funnel: register -> book -> attend. SQLite cannot widen a
-- CHECK constraint in place, so the table is rebuilt and the rows copied across.
-- Still no triggers.

CREATE TABLE outcome_events_new (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL CHECK (kind IN (
                   'booking_submitted', 'booking_delivered', 'booking_delivery_failed',
                   'call_click', 'instagram_click',
                   'patient_registered', 'appointment_booked', 'followup_booked',
                   'session_attended', 'attendance_confirmed')),
    booking_id INTEGER NULL REFERENCES bookings(id) ON DELETE SET NULL,
    meta       TEXT    NOT NULL DEFAULT '{}' CHECK (length(meta) <= 2000),
    day        TEXT    NOT NULL CHECK (length(day) = 10),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO outcome_events_new (id, kind, booking_id, meta, day, created_at)
SELECT id, kind, booking_id, meta, day, created_at FROM outcome_events;

DROP TABLE outcome_events;

ALTER TABLE outcome_events_new RENAME TO outcome_events;

CREATE INDEX idx_outcome_day ON outcome_events(day, kind);
