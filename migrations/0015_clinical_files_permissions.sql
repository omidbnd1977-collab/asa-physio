-- 0015_clinical_files_permissions.sql — forward only.
-- Extends staff_permissions CHECK with clinical.record and files.manage.
-- Does not alter migrations 0010–0014. Existing grants are preserved; new keys
-- are never auto-assigned to secretaries (owner must grant explicitly).

CREATE TABLE staff_permissions_new (
    user_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    permission TEXT    NOT NULL CHECK (permission IN (
                   'patients.view', 'patients.create', 'patients.edit',
                   'appointments.view', 'appointments.create',
                   'appointments.move', 'appointments.cancel',
                   'messages.view', 'reception.queue',
                   'clinical.record', 'files.manage')),
    PRIMARY KEY (user_id, permission)
);
INSERT INTO staff_permissions_new (user_id, permission)
SELECT user_id, permission FROM staff_permissions;
DROP TABLE staff_permissions;
ALTER TABLE staff_permissions_new RENAME TO staff_permissions;
