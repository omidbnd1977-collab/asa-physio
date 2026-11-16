-- Record explicit, optional consent for service-related patient SMS only.
-- Existing patients default to no consent; no historic consent is inferred.
ALTER TABLE patients ADD COLUMN sms_service_consent INTEGER NOT NULL DEFAULT 0
    CHECK (sms_service_consent IN (0, 1));
ALTER TABLE patients ADD COLUMN sms_service_consent_at TEXT NULL;
ALTER TABLE patients ADD COLUMN sms_service_consent_version TEXT NOT NULL DEFAULT '';
