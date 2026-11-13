-- 0011_admin_sms_claim.sql — forward only.
-- One admin_appointment SMS per appointment + atomic send claim (status=sending).
-- Builds on 0010; copies all existing sms_messages rows (deduped) without data loss.

-- Prefer a sent row when duplicates exist; otherwise keep the lowest id.
DELETE FROM sms_messages
WHERE kind = 'admin_appointment'
  AND appointment_id IS NOT NULL
  AND status != 'sent'
  AND EXISTS (
      SELECT 1 FROM sms_messages AS s
      WHERE s.kind = 'admin_appointment'
        AND s.appointment_id = sms_messages.appointment_id
        AND s.status = 'sent'
  );

DELETE FROM sms_messages
WHERE kind = 'admin_appointment'
  AND appointment_id IS NOT NULL
  AND id NOT IN (
      SELECT MIN(id) FROM sms_messages
      WHERE kind = 'admin_appointment' AND appointment_id IS NOT NULL
      GROUP BY appointment_id
  );

CREATE TABLE sms_messages_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id     INTEGER NULL REFERENCES patients(id) ON DELETE SET NULL,
    appointment_id INTEGER NULL REFERENCES appointments(id) ON DELETE SET NULL,
    phone          TEXT    NOT NULL
                           CHECK (phone GLOB '09[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    kind           TEXT    NOT NULL CHECK (kind IN (
                       'registered', 'booked', 'rescheduled', 'cancelled',
                       'followup', 'confirm_request', 'custom',
                       'welcome_back', 'session_logged', 'admin_appointment')),
    body           TEXT    NOT NULL CHECK (length(body) BETWEEN 1 AND 600),
    status         TEXT    NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    provider       TEXT    NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
    last_error     TEXT    NOT NULL DEFAULT '' CHECK (length(last_error) <= 400),
    sent_at        TEXT    NULL,
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT INTO sms_messages_new (
    id, patient_id, appointment_id, phone, kind, body, status, provider,
    attempts, last_error, sent_at, created_at, updated_at
)
SELECT id, patient_id, appointment_id, phone, kind, body, status, provider,
       attempts, last_error, sent_at, created_at, created_at
FROM sms_messages;
DROP TABLE sms_messages;
ALTER TABLE sms_messages_new RENAME TO sms_messages;
CREATE INDEX idx_sms_patient ON sms_messages(patient_id, created_at DESC);
CREATE INDEX idx_sms_status  ON sms_messages(status, created_at);
-- Enforces a single admin alert row per appointment (NULL appointment_id excluded).
CREATE UNIQUE INDEX idx_sms_admin_appointment_unique
    ON sms_messages(appointment_id)
    WHERE kind = 'admin_appointment' AND appointment_id IS NOT NULL;
