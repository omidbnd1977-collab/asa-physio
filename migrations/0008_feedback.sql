-- 0008_feedback.sql — public feedback, private by default; publication is staff-controlled.
CREATE TABLE feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id  TEXT NOT NULL UNIQUE CHECK (length(public_id) = 12),
    name       TEXT NOT NULL DEFAULT '' CHECK (length(name) <= 80),
    category   TEXT NOT NULL CHECK (category IN ('satisfaction','suggestion','complaint')),
    body       TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 5 AND 2000),
    rating     INTEGER NULL CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    published  INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0,1)),
    ip_fp      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_feedback_created ON feedback(created_at DESC);
CREATE INDEX idx_feedback_published ON feedback(published, created_at DESC);
