-- 0009_clinic_closed_days.sql — forward only.
-- Explicit clinic-wide closure dates, managed by staff. Existing appointments are not modified.
CREATE TABLE clinic_closed_days (
    slot_date  TEXT PRIMARY KEY CHECK (length(slot_date) = 10),
    reason     TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 200),
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
