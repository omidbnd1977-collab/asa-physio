-- 0003_patients_appointments.sql — forward only.
-- Patient registration, returning-patient login, the appointment grid, and SMS.
-- No triggers: the cabin allocation and the capacity rule live in api/scheduling.py,
-- but the UNIQUE constraint below is what actually makes double-booking impossible.

CREATE TABLE patients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id      TEXT    NOT NULL UNIQUE CHECK (length(public_id) = 12),
    national_id    TEXT    NOT NULL UNIQUE
                           CHECK (national_id GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    full_name      TEXT    NOT NULL CHECK (length(trim(full_name)) BETWEEN 3 AND 80),
    birth_date     TEXT    NOT NULL CHECK (length(birth_date) = 10),   -- gregorian ISO
    birth_jalali   TEXT    NOT NULL CHECK (length(birth_jalali) = 10), -- display only
    phone          TEXT    NOT NULL
                           CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    mri_link       TEXT    NOT NULL DEFAULT '' CHECK (length(mri_link) <= 500),
    ortho_doctor   TEXT    NOT NULL DEFAULT '' CHECK (length(ortho_doctor) <= 80),
    med_photo      TEXT    NOT NULL DEFAULT '' CHECK (length(med_photo) <= 120),
    staff_note     TEXT    NOT NULL DEFAULT '' CHECK (length(staff_note) <= 2000),
    is_blocked     INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1)),
    ip_fp          TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_patients_phone   ON patients(phone);
CREATE INDEX idx_patients_created ON patients(created_at DESC);

CREATE TABLE patient_sessions (
    id         TEXT    PRIMARY KEY CHECK (length(id) = 64),
    patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at TEXT    NOT NULL,
    ip_fp      TEXT    NOT NULL DEFAULT '',
    revoked_at TEXT    NULL
);
CREATE INDEX idx_psessions_patient ON patient_sessions(patient_id);

-- Clinic hours: 16:00 to 22:00, one slot every 30 minutes, 10 cabins.
-- 12 start times x 10 cabins = 120 appointments per day.
-- The UNIQUE below is the real guarantee: one cabin cannot hold two patients
-- at the same minute, no matter how many requests race.
CREATE TABLE appointments (
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
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (slot_date, slot_time, cabin)
);
CREATE INDEX idx_appt_slot    ON appointments(slot_date, slot_time);
CREATE INDEX idx_appt_patient ON appointments(patient_id, slot_date DESC);
CREATE INDEX idx_appt_confirm ON appointments(status, confirm_sent_at);

CREATE TABLE sms_messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NULL REFERENCES patients(id) ON DELETE SET NULL,
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    phone          TEXT    NOT NULL
                           CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'registered', 'booked', 'rescheduled', 'cancelled',
                       'followup', 'confirm_request', 'custom')),
    body           TEXT    NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    status         TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sent', 'failed')),
    provider       TEXT    NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
    last_error     TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 400),
    sent_at        TEXT    NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_sms_patient ON sms_messages(patient_id, created_at DESC);
CREATE INDEX idx_sms_status  ON sms_messages(status, created_at);

CREATE TABLE sms_inbound (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phone          TEXT    NOT NULL,
    body           TEXT    NOT NULL CHECK (length(body) <= 300),
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    handled        TEXT    NOT NULL DEFAULT 'ignored'
                           CHECK (handled IN ('confirmed', 'declined', 'ignored', 'unknown_phone')),
    received_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_inbound_phone ON sms_inbound(phone, received_at DESC);
