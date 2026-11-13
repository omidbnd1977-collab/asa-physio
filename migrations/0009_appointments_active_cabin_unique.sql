-- 0009_appointments_active_cabin_unique.sql — forward only.
-- Cancelled appointments must keep their history but must not keep occupying a cabin.
-- SQLite table-level UNIQUE cannot be partial, so the appointments table is rebuilt
-- without UNIQUE(slot_date, slot_time, cabin) and a partial unique index replaces it:
-- only non-cancelled rows block a cabin. All existing rows are copied unchanged.
-- Foreign keys are disabled by the migration runner around this file (see api/db.py).

CREATE TABLE appointments_new (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id    TEXT    NOT NULL UNIQUE CHECK (length(public_id) = 10),
    patient_id   INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    slot_date    TEXT    NOT NULL CHECK (length(slot_date) = 10),
    slot_time    TEXT    NOT NULL CHECK (slot_time GLOB '[0-9][0-9]:[0-9][0-9]'),
    cabin        INTEGER NOT NULL CHECK (cabin BETWEEN 1 AND 10),
    status       TEXT    NOT NULL DEFAULT 'booked'
                         CHECK (status IN ('booked', 'cancelled', 'attended', 'no_show')),
    kind         TEXT    NOT NULL DEFAULT 'first'
                         CHECK (kind IN ('first', 'followup')),
    booked_by    TEXT    NOT NULL DEFAULT 'patient'
                         CHECK (booked_by IN ('patient', 'staff')),
    attendance   TEXT    NOT NULL DEFAULT 'unknown'
                         CHECK (attendance IN ('unknown', 'coming', 'not_coming')),
    confirm_sent_at TEXT NULL,
    confirmed_at    TEXT NULL,
    moved_from   TEXT    NOT NULL DEFAULT '' CHECK (length(moved_from) <= 32),
    note         TEXT    NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO appointments_new (
    id, public_id, patient_id, slot_date, slot_time, cabin, status, kind, booked_by,
    attendance, confirm_sent_at, confirmed_at, moved_from, note, created_at, updated_at
)
SELECT
    id, public_id, patient_id, slot_date, slot_time, cabin, status, kind, booked_by,
    attendance, confirm_sent_at, confirmed_at, moved_from, note, created_at, updated_at
FROM appointments;

DROP TABLE appointments;

ALTER TABLE appointments_new RENAME TO appointments;

CREATE INDEX idx_appt_slot    ON appointments(slot_date, slot_time);
CREATE INDEX idx_appt_patient ON appointments(patient_id, slot_date DESC);
CREATE INDEX idx_appt_confirm ON appointments(status, confirm_sent_at);
CREATE UNIQUE INDEX idx_appt_active_cabin
    ON appointments(slot_date, slot_time, cabin)
    WHERE status != 'cancelled';
