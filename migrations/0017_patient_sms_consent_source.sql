-- Where explicit service-SMS consent was recorded (register vs later booking step).
-- Empty string means no consent has been granted yet.
ALTER TABLE patients ADD COLUMN sms_service_consent_source TEXT NOT NULL DEFAULT ''
    CHECK (length(sms_service_consent_source) <= 40);
