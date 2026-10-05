-- 0006_audit_history.sql — forward only.
-- Durable audit trail for protected patient and appointment changes.
-- No triggers: audit records are written by the application service layer.

CREATE TABLE audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_role  TEXT    NOT NULL CHECK (actor_role IN ('system', 'staff', 'owner', 'patient')),
    actor_id    INTEGER NULL,
    action      TEXT    NOT NULL CHECK (length(action) BETWEEN 2 AND 80),
    entity      TEXT    NOT NULL CHECK (length(entity) BETWEEN 2 AND 80),
    entity_id   TEXT    NOT NULL CHECK (length(entity_id) BETWEEN 1 AND 80),
    before_json TEXT    NOT NULL DEFAULT '{}' CHECK (length(before_json) <= 20000),
    after_json  TEXT    NOT NULL DEFAULT '{}' CHECK (length(after_json) <= 20000),
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX idx_audit_entity ON audit_logs(entity, entity_id, created_at DESC);
CREATE INDEX idx_audit_actor ON audit_logs(actor_role, actor_id, created_at DESC);
