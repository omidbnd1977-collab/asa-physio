"""Layer 02 + 08 — validation at the boundary. Every input is assumed hostile."""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Persian / Arabic-Indic digits -> ASCII
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_CTRL = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u200e\u200f\u202a-\u202e]")
_WS = re.compile(r"[ \t\u00a0\u200c]{2,}")

SERVICES = ("laser", "shockwave", "tecar", "manual", "acupuncture", "consult", "rehab", "other")
# what the existing <select> on the site sends, mapped onto the enum above
SERVICE_ALIASES = {
    "لیزر پرتوان": "laser",
    "شاک ویو": "shockwave",
    "تکار تراپی": "tecar",
    "تکار": "tecar",
    "درمان‌های دستی": "manual",
    "درمان های دستی": "manual",
    "طب سوزنی": "acupuncture",
    "مشاوره رایگان": "consult",
    "مشاوره": "consult",
    "توانبخشی": "rehab",
    "توانبخشی بعد از جراحی": "rehab",
    "خدمت مورد نظر": "other",
    "سایر": "other",
    "": "other",
}


def clean_text(value: str, *, max_len: int) -> str:
    v = unicodedata.normalize("NFC", str(value))
    v = _CTRL.sub("", v)
    v = _WS.sub(" ", v).strip()
    return v[:max_len]


class BookingIn(BaseModel):
    """Mirrors the form already on the site; no field was added or removed."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=10, max_length=20)
    service: str = Field(default="other", max_length=60)
    note: str = Field(default="", max_length=1000)
    # honeypot: real users never fill this, bots do
    website: str = Field(default="", max_length=100)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v, max_len=80)
        if len(v) < 2:
            raise ValueError("نام را کامل وارد کنید.")
        if re.search(r"https?://|<[^>]+>", v):
            raise ValueError("نام نمی‌تواند شامل لینک یا کد باشد.")
        if not re.match(r"^[\w\u0600-\u06FF\u200c .'\-]+$", v, re.UNICODE):
            raise ValueError("نام فقط می‌تواند شامل حروف و فاصله باشد.")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        raw = clean_text(v, max_len=20).translate(_DIGITS)
        raw = re.sub(r"[^\d+]", "", raw)
        if raw.startswith("+98"):
            raw = "0" + raw[3:]
        elif raw.startswith("0098"):
            raw = "0" + raw[4:]
        elif raw.startswith("98") and len(raw) == 12:
            raw = "0" + raw[2:]
        elif raw.startswith("9") and len(raw) == 10:
            raw = "0" + raw
        if not re.fullmatch(r"09\d{9}", raw):
            raise ValueError("شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.")
        return raw

    @field_validator("service")
    @classmethod
    def _service(cls, v: str) -> str:
        v = clean_text(v, max_len=60)
        if v in SERVICES:
            return v
        return SERVICE_ALIASES.get(v, "other")

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        v = clean_text(v, max_len=1000)
        if re.search(r"<\s*(script|iframe|object|embed)", v, re.I):
            raise ValueError("متن نامعتبر است.")
        if len(re.findall(r"https?://", v)) > 2:
            raise ValueError("تعداد لینک‌های متن بیش از حد مجاز است.")
        return v


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=8, max_length=256)


class BookingStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(new|contacted|scheduled|done|spam)$")


class OutcomeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern=r"^(call_click|instagram_click)$")


class BookingOut(BaseModel):
    public_id: str
    status: str
    created_at: str
    delivered: bool


# ==========================================================================
# Patient portal
# ==========================================================================
class PatientRegisterIn(BaseModel):
    """First visit. کد ملی is collected here because the returning-patient login
    matches on it — without it a patient could never come back in."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=3, max_length=80)
    national_id: str = Field(min_length=8, max_length=14)
    birth_date: str = Field(min_length=6, max_length=12)  # Jalali y/m/d
    phone: str = Field(min_length=10, max_length=20)
    mri_link: str = Field(default="", max_length=500)
    ortho_doctor: str = Field(default="", max_length=80)
    website: str = Field(default="", max_length=100)  # honeypot

    @field_validator("full_name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v, max_len=80)
        if len(v.split()) < 2:
            raise ValueError("نام و نام خانوادگی را کامل وارد کنید.")
        if not re.match(r"^[\w\u0600-\u06FF\u200c .'\-]+$", v, re.UNICODE):
            raise ValueError("نام فقط می‌تواند شامل حروف و فاصله باشد.")
        return v

    @field_validator("national_id")
    @classmethod
    def _nid(cls, v: str) -> str:
        from .jalali import normalize_national_id, valid_national_id

        nid = normalize_national_id(v)
        if not valid_national_id(nid):
            raise ValueError("کد ملی معتبر نیست.")
        return nid

    @field_validator("birth_date")
    @classmethod
    def _birth(cls, v: str) -> str:
        from .jalali import parse_jalali, today_tehran

        d = parse_jalali(v)
        today = today_tehran()
        age = (today - d).days / 365.25
        if not (1 <= age <= 120):
            raise ValueError("تاریخ تولد منطقی نیست.")
        return d.isoformat()

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return BookingIn._phone(v)

    @field_validator("mri_link")
    @classmethod
    def _mri(cls, v: str) -> str:
        v = clean_text(v, max_len=500)
        if not v:
            return ""
        if not re.match(r"^https?://[\w.\-]+\.[a-z]{2,}(/\S*)?$", v, re.I):
            raise ValueError("لینک MRI باید با http:// یا https:// شروع شود.")
        return v

    @field_validator("ortho_doctor")
    @classmethod
    def _ortho(cls, v: str) -> str:
        v = clean_text(v, max_len=80)
        if v and not re.match(r"^[\w\u0600-\u06FF\u200c .'\-]+$", v, re.UNICODE):
            raise ValueError("نام پزشک فقط می‌تواند شامل حروف و فاصله باشد.")
        return v


class PatientLoginIn(BaseModel):
    """Returning patient: national id + date of birth must match the registration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    national_id: str = Field(min_length=8, max_length=14)
    birth_date: str = Field(min_length=6, max_length=12)

    @field_validator("national_id")
    @classmethod
    def _nid(cls, v: str) -> str:
        from .jalali import normalize_national_id

        return normalize_national_id(v)

    @field_validator("birth_date")
    @classmethod
    def _birth(cls, v: str) -> str:
        from .jalali import parse_jalali

        return parse_jalali(v).isoformat()


class AppointmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    note: str = Field(default="", max_length=500)

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        return clean_text(v, max_len=500)


class AppointmentMoveIn(BaseModel):
    """Staff reschedules an appointment."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    notify: bool = True


class FollowUpIn(BaseModel):
    """Staff books the next session for an existing patient."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    note: str = Field(default="", max_length=500)
    notify: bool = True


class AppointmentStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(booked|cancelled|attended|no_show)$")
    notify: bool = False


class PatientNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    staff_note: str = Field(default="", max_length=2000)

    @field_validator("staff_note")
    @classmethod
    def _n(cls, v: str) -> str:
        return clean_text(v, max_len=2000)


class MessageIn(BaseModel):
    """One message in the doctor<->patient thread (either direction)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    body: str = Field(min_length=1, max_length=1200)
    notify: bool = False  # staff only: also send the reply by SMS

    @field_validator("body")
    @classmethod
    def _b(cls, v: str) -> str:
        v = clean_text(v, max_len=1000)
        if len(v) < 2:
            raise ValueError("متن پیام خیلی کوتاه است.")
        if re.search(r"<\s*(script|iframe|object|embed)", v, re.I):
            raise ValueError("متن نامعتبر است.")
        if len(re.findall(r"https?://", v)) > 2:
            raise ValueError("تعداد لینک‌های متن بیش از حد مجاز است.")
        return v


class SmsInboundIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    phone: str = Field(min_length=5, max_length=20)
    body: str = Field(default="", max_length=300)

    @field_validator("phone")
    @classmethod
    def _p(cls, v: str) -> str:
        return BookingIn._phone(v)

    @field_validator("body")
    @classmethod
    def _b(cls, v: str) -> str:
        return clean_text(v, max_len=300)


# ==========================================================================
# Clinical record (doctor panel)
# ==========================================================================
class CatalogItemIn(BaseModel):
    """A clinician typed something that was not in the dropdown."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=60)
    category: str = Field(default="other", pattern=r"^(spine|upper|lower|neuro|other)$")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = clean_text(v, max_len=60)
        if len(v) < 2:
            raise ValueError("عنوان خیلی کوتاه است.")
        if re.search(r"<[^>]+>|https?://", v):
            raise ValueError("عنوان نمی‌تواند شامل لینک یا کد باشد.")
        return v


class TreatmentSessionIn(BaseModel):
    """What the clinician actually did in this session."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    body_part_ids: list[int] = Field(min_length=1, max_length=20)
    treatment_ids: list[int] = Field(min_length=1, max_length=20)
    findings: str = Field(default="", max_length=2000)
    plan: str = Field(default="", max_length=2000)
    pain_before: int | None = Field(default=None, ge=0, le=10)
    pain_after: int | None = Field(default=None, ge=0, le=10)
    appointment_id: str = Field(default="", max_length=10)
    session_date: str = Field(default="", max_length=10)
    mark_attended: bool = True
    notify: bool = False

    @field_validator("findings", "plan")
    @classmethod
    def _txt(cls, v: str) -> str:
        return clean_text(v, max_len=2000)

    @field_validator("session_date")
    @classmethod
    def _day(cls, v: str) -> str:
        if v and not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("تاریخ نامعتبر است.")
        return v
