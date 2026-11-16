-- 0014_reception_queue.sql — forward only.
-- 1) Ownership columns for consult leads and inbound SMS (unassigned = owner_id NULL).
-- 2) Extend staff_permissions CHECK to allow reception.queue.
-- Does not alter migrations 0010–0013.

ALTER TABLE bookings ADD COLUMN owner_id INTEGER
    REFERENCES admin_users(id) ON DELETE SET NULL;
CREATE INDEX idx_bookings_owner ON bookings(owner_id);

ALTER TABLE sms_inbound ADD COLUMN patient_id INTEGER
    REFERENCES patients(id) ON DELETE SET NULL;
ALTER TABLE sms_inbound ADD COLUMN owner_id INTEGER
    REFERENCES admin_users(id) ON DELETE SET NULL;
CREATE INDEX idx_inbound_owner ON sms_inbound(owner_id);
CREATE INDEX idx_inbound_patient ON sms_inbound(patient_id);

-- SQLite cannot ALTER a CHECK; rebuild staff_permissions with the new key.
CREATE TABLE staff_permissions_new (
    user_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    permission TEXT    NOT NULL CHECK (permission IN (
                   'patients.view', 'patients.create', 'patients.edit',
                   'appointments.view', 'appointments.create',
                   'appointments.move', 'appointments.cancel',
                   'messages.view', 'reception.queue')),
    PRIMARY KEY (user_id, permission)
);
INSERT INTO staff_permissions_new (user_id, permission)
SELECT user_id, permission FROM staff_permissions;
DROP TABLE staff_permissions;
ALTER TABLE staff_permissions_new RENAME TO staff_permissions;
