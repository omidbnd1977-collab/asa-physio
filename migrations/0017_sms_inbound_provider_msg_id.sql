-- Deduplicate SMS.ir inbound callbacks / polls by provider message id.
-- NULL allowed for legacy rows and manual JSON posts without an id.
ALTER TABLE sms_inbound ADD COLUMN provider_msg_id TEXT NULL;
CREATE UNIQUE INDEX idx_sms_inbound_provider_msg_id
    ON sms_inbound(provider_msg_id)
    WHERE provider_msg_id IS NOT NULL;
