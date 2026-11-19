"""Registration consent text + version for portal signup audit trail.

Stores only version id, SHA-256 of the canonical consent sentence, and acceptance
time — not medical payloads or full page HTML.
"""

from __future__ import annotations

import hashlib

# Bump when the checkbox sentence changes; keep in sync with portal UI value.
REGISTRATION_CONSENT_VERSION = "registration-consent-v1"

REGISTRATION_CONSENT_TEXT = (
    "با علامت‌گذاری این گزینه تأیید می‌کنم که صفحات «حریم خصوصی» و «قوانین استفاده» "
    "سامانه آسا فیزیو را مطالعه کرده‌ام و با ثبت اطلاعات هویتی و پزشکی خود در این "
    "سامانه موافقم."
)

REGISTRATION_CONSENT_SOURCE = "portal_register"


def consent_text_hash(text: str = REGISTRATION_CONSENT_TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_truthy_accepted(raw: str | bool | int | None) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw == 1
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
