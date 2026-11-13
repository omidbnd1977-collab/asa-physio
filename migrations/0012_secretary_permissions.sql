-- 0012_secretary_permissions.sql — forward only.
-- Doctors (role=owner) manage clinic secretaries (role=staff) with granular permissions.
-- Extends admin_users with full_name + created_by; does not alter SMS migrations 0010/0011.

CREATE TABLE admin_users_new (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE
                          CHECK (length(username) BETWEEN 3 AND 32),
    password_hash TEXT    NOT NULL CHECK (length(password_hash) >= 20),
    full_name     TEXT    NOT NULL DEFAULT ''
                          CHECK (length(trim(full_name)) BETWEEN 0 AND 80),
    role          TEXT    NOT NULL DEFAULT 'staff'
                          CHECK (role IN ('owner', 'staff')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_by    INTEGER NULL REFERENCES admin_users_new(id) ON DELETE SET NULL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login_at TEXT    NULL
);

-- SQLite: self-FK to old table during copy is awkward; insert without created_by first.
INSERT INTO admin_users_new (
    id, username, password_hash, full_name, role, is_active, created_by, created_at, last_login_at
)
SELECT id, username, password_hash, username, role, is_active, NULL, created_at, last_login_at
FROM admin_users;

DROP TABLE admin_users;
ALTER TABLE admin_users_new RENAME TO admin_users;

-- Recreate FK from sessions (still valid by id); ensure index-less uniqueness on username remains.
CREATE TABLE staff_permissions (
    user_id    INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    permission TEXT    NOT NULL CHECK (permission IN (
                   'patients.view', 'patients.create', 'patients.edit',
                   'appointments.view', 'appointments.create',
                   'appointments.move', 'appointments.cancel',
                   'messages.view')),
    PRIMARY KEY (user_id, permission)
);
CREATE INDEX idx_staff_permissions_user ON staff_permissions(user_id);
