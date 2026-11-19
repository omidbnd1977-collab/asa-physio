"""Registration consent text + version for portal signup audit trail.

Stores only version id, SHA-256 of the canonical consent sentence, and acceptance
time — not medical payloads or full page HTML.

REGISTRATION_CONSENT_TEXT is the single source of truth: the portal checkbox shows
exactly this sentence (with privacy/terms phrases wrapped as links for navigation),
and consent_text_hash() hashes the same plain string.
"""

from __future__ import annotations

import hashlib

# Bump when REGISTRATION_CONSENT_TEXT changes.
REGISTRATION_CONSENT_VERSION = "registration-consent-v1"

REGISTRATION_CONSENT_TEXT = (
    "با علامت‌گذاری این گزینه تأیید می‌کنم که صفحات «حریم خصوصی» و «قوانین استفاده» "
    "سامانه آسا فیزیو را مطالعه کرده‌ام و با ثبت اطلاعات هویتی و پزشکی خود در این "
    "سامانه موافقم."
)

REGISTRATION_CONSENT_SOURCE = "portal_register"


def consent_text_hash(text: str = REGISTRATION_CONSENT_TEXT) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def registration_consent_label_html() -> str:
    """Checkbox label HTML: same visible text as REGISTRATION_CONSENT_TEXT + legal links."""
    return REGISTRATION_CONSENT_TEXT.replace(
        "«حریم خصوصی»",
        '«<a href="/privacy.html" target="_blank" rel="noopener">حریم خصوصی</a>»',
    ).replace(
        "«قوانین استفاده»",
        '«<a href="/terms.html" target="_blank" rel="noopener">قوانین استفاده</a>»',
    )


def is_truthy_accepted(raw: str | bool | int | None) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw == 1
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}
