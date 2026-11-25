"""Client-side registration wizard error presentation (step + focus + flash).

Server consent enforcement stays in patients.py; this module only documents the
browser UX rules so tests can assert step selection without executing JS.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# Same wording as portal flash / field error and API consent_required message.
REGISTRATION_CONSENT_REQUIRED_MSG = (
    "لطفاً برای ادامه ثبت‌نام، ابتدا گزینه تأیید مطالعه قوانین "
    "و حریم خصوصی و موافقت با ثبت اطلاعات را علامت بزنید."
)

REGISTER_STEP1_FIELDS = frozenset(
    {
        "full_name",
        "national_code",
        "birth_year",
        "birth_month",
        "birth_day",
        "mobile",
    }
)

FlashKind = Literal["consent_only", "fields_only", "fields_and_consent"]


class RegistrationClientErrorPlan(TypedDict):
    step: int
    focus: str
    flash: FlashKind
    scroll_consent: bool


def registration_client_error_plan(
    first_field: str,
    *,
    consent_missing: bool,
) -> RegistrationClientErrorPlan:
    """Decide wizard step / focus / flash for a failed client-side register check.

    ``first_field`` is the first invalid control id (same order as portal JS).
    Consent may be missing even when ``first_field`` is another control.
    """
    if consent_missing and first_field == "consent_accepted":
        return {
            "step": 2,
            "focus": "consent_accepted",
            "flash": "consent_only",
            "scroll_consent": True,
        }
    if first_field in REGISTER_STEP1_FIELDS:
        return {
            "step": 1,
            "focus": first_field,
            "flash": "fields_and_consent" if consent_missing else "fields_only",
            # Never jump to step 2: that would hide step-1 field errors.
            "scroll_consent": False,
        }
    return {
        "step": 2,
        "focus": first_field,
        "flash": "fields_and_consent" if consent_missing else "fields_only",
        "scroll_consent": False,
    }
