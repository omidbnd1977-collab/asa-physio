-- 0013_owner_isolation.sql — forward only.
-- Multi-owner isolation for patients + real staff actor on appointments.
-- Does NOT auto-assign owner_id or booked_by_user_id (legacy rows stay NULL).
-- Leaves migrations 0010–0012 untouched.

ALTER TABLE patients ADD COLUMN owner_id INTEGER
    REFERENCES admin_users(id) ON DELETE SET NULL;
CREATE INDEX idx_patients_owner ON patients(owner_id);

ALTER TABLE appointments ADD COLUMN booked_by_user_id INTEGER
    REFERENCES admin_users(id) ON DELETE SET NULL;
CREATE INDEX idx_appt_booked_by_user ON appointments(booked_by_user_id);
