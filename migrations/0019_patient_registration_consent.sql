-- 0017_patient_registration_consent.sql — forward only.
-- Runs after 0016_sms_status_logged.sql.
-- Audit fields for portal registration consent (version + hash + time).
-- Existing patients keep empty strings; new registrations must set non-empty values.

ALTER TABLE patients ADD COLUMN consent_version TEXT NOT NULL DEFAULT '';
ALTER TABLE patients ADD COLUMN consent_accepted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE patients ADD COLUMN consent_text_hash TEXT NOT NULL DEFAULT '';

CREATE TABLE patient_consent_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id         INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
    consent_version    TEXT    NOT NULL
                               CHECK (length(trim(consent_version)) BETWEEN 1 AND 64),
    consent_text_hash  TEXT    NOT NULL CHECK (length(consent_text_hash) = 64),
    accepted_at        TEXT    NOT NULL,
    source             TEXT    NOT NULL DEFAULT 'portal_register'
                               CHECK (source IN ('portal_register')),
    ip_fp              TEXT    NOT NULL DEFAULT '',
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_consent_events_patient ON patient_consent_events(patient_id);
CREATE INDEX idx_consent_events_accepted ON patient_consent_events(accepted_at DESC);
