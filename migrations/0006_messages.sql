-- 0006_messages.sql — forward only.
-- The in-app channel that ties the doctor panel and the patient portal together:
-- patients can write to the clinic, staff can reply, and every appointment event
-- (book, move, cancel, next session, recorded treatment) leaves a note in the same
-- thread, so both sides always see the same story.
-- Still no triggers.

CREATE TABLE messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id          TEXT    NOT NULL UNIQUE CHECK (length(public_id) = 12),
    patient_id         INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    appointment_id     INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    -- 'patient': written by the patient (or an automatic note about a patient action,
    --  e.g. a cancellation, so the staff inbox badge still catches it)
    -- 'staff':   written from the doctor panel
    -- 'system':  an automatic note about a clinic action (move, cancel, follow-up, …)
    sender             TEXT    NOT NULL CHECK (sender IN ('patient', 'staff', 'system')),
    kind               TEXT    NOT NULL DEFAULT 'chat'
                               CHECK (kind IN ('chat', 'booked', 'rescheduled',
                                               'cancelled', 'followup', 'session')),
    body               TEXT    NOT NULL CHECK (length(trim(body)) BETWEEN 2 AND 1000),
    created_by         INTEGER NULL REFERENCES admin_users(id) ON DELETE SET NULL,
    read_by_patient_at TEXT    NULL,
    read_by_staff_at   TEXT    NULL,
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_msg_patient       ON messages(patient_id, created_at);
CREATE INDEX idx_msg_unread_staff  ON messages(read_by_staff_at)   WHERE sender = 'patient';
CREATE INDEX idx_msg_unread_client ON messages(patient_id, read_by_patient_at)
    WHERE sender IN ('staff', 'system');
