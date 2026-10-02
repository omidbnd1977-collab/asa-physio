-- 0001_init.sql — forward only. Never edit a migration that has been applied; add a new one.
-- Layer 03: every column states NOT NULL / DEFAULT / CHECK explicitly. No triggers: zero
-- business logic hides in this file, all of it lives in api/repo.py.

CREATE TABLE admin_users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE
                          CHECK (length(username) BETWEEN 3 AND 32),
    password_hash TEXT    NOT NULL CHECK (length(password_hash) >= 20),
    role          TEXT    NOT NULL DEFAULT 'staff'
                          CHECK (role IN ('owner', 'staff')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login_at TEXT    NULL
);

CREATE TABLE sessions (
    id          TEXT    PRIMARY KEY CHECK (length(id) = 64),
    user_id     INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at  TEXT    NOT NULL,
    ip_fp       TEXT    NOT NULL DEFAULT '',
    revoked_at  TEXT    NULL
);
CREATE INDEX idx_sessions_user    ON sessions(user_id);
CREATE INDEX idx_sessions_expires ON sessions(expires_at);

CREATE TABLE bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id   TEXT    NOT NULL UNIQUE CHECK (length(public_id) = 12),
    name        TEXT    NOT NULL CHECK (length(trim(name)) BETWEEN 2 AND 80),
    phone       TEXT    NOT NULL CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    service     TEXT    NOT NULL CHECK (service IN (
                    'laser', 'shockwave', 'tecar', 'manual', 'acupuncture',
                    'consult', 'rehab', 'other')),
    note        TEXT    NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    status      TEXT    NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'contacted', 'scheduled', 'done', 'spam')),
    source      TEXT    NOT NULL DEFAULT 'web' CHECK (source IN ('web', 'phone', 'walk_in')),
    ip_fp       TEXT    NOT NULL DEFAULT '',
    ua_fp       TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_bookings_created ON bookings(created_at DESC);
CREATE INDEX idx_bookings_status  ON bookings(status);
CREATE INDEX idx_bookings_phone   ON bookings(phone);

-- Layer 02: replayed POSTs must not create a second booking.
CREATE TABLE idempotency_keys (
    key          TEXT    PRIMARY KEY CHECK (length(key) BETWEEN 8 AND 128),
    scope        TEXT    NOT NULL CHECK (length(scope) <= 64),
    request_hash TEXT    NOT NULL CHECK (length(request_hash) = 64),
    status_code  INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
    response     TEXT    NOT NULL CHECK (length(response) <= 65536),
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_idem_created ON idempotency_keys(created_at);

-- Layer 09: limits survive a restart and are shared by every worker.
CREATE TABLE rate_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket   TEXT    NOT NULL CHECK (length(bucket) <= 160),
    at       REAL    NOT NULL CHECK (at > 0)
);
CREATE INDEX idx_rate_bucket ON rate_events(bucket, at);

CREATE TABLE breaker_state (
    name       TEXT PRIMARY KEY,
    open_until REAL NOT NULL DEFAULT 0 CHECK (open_until >= 0),
    reason     TEXT NOT NULL DEFAULT ''
);

-- Layer 06: one row per paid call, so the monthly bill is never a surprise.
CREATE TABLE cost_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    resource   TEXT    NOT NULL CHECK (length(resource) <= 48),
    units      REAL    NOT NULL CHECK (units >= 0),
    unit_cost  REAL    NOT NULL CHECK (unit_cost >= 0),
    cost_usd   REAL    NOT NULL CHECK (cost_usd >= 0),
    month      TEXT    NOT NULL CHECK (length(month) = 7),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_cost_month ON cost_events(month, resource);

-- Layer 14: the one number. Recorded from the first request, never derived later.
CREATE TABLE outcome_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT    NOT NULL CHECK (kind IN (
                   'booking_submitted', 'booking_delivered', 'booking_delivery_failed',
                   'call_click', 'instagram_click')),
    booking_id INTEGER NULL REFERENCES bookings(id) ON DELETE SET NULL,
    meta       TEXT    NOT NULL DEFAULT '{}' CHECK (length(meta) <= 2000),
    day        TEXT    NOT NULL CHECK (length(day) = 10),
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_outcome_day ON outcome_events(day, kind);
