"""سیاست دسترسی صریح هر جدول (بخش «امنیت داده‌ی بیمار» در BOOKING.md).

هر قرائت/نوشت در برنامه از `authorize()` رد می‌شود. نقش‌ها:
  anonymous  : بیمارِ لاگین‌نکرده — عملاً چیزی نمی‌خواند
  patient    : فقط رکوردهای خودش، و فقط فیلدهای `patient_fields`
  staff      : کارکنان پنل (پزشک/منشی)
  physician  : پزشک — فیلدهای بالینی و نوشت در جدول‌های درمان

`GET /api/admin/patients/{id}/photo|mri` دقیقاً با همین ماژول قفل می‌شود.
"""
from __future__ import annotations

POLICIES: dict[str, dict] = {
    "patients": dict(
        read=("staff", "physician"),
        write=("physician",),
        self_read=("name", "birth_jdate", "mobile", "created_at"),
        public=("id",),
        note="کد ملی و تاریخ تولد هرگز به کلاینت بیمار برنمی‌گردند.",
    ),
    "appointments": dict(
        read=("staff", "physician"),
        write=("patient", "physician", "staff"),
        self_read=("id", "slot_date", "slot_time", "cabin", "status", "tracking_code",
                   "note", "orthopedist", "mri_url"),
        note="بیمار فقط نوبت‌های خودش؛ مالکیت با patient_id در کوئری اعمال می‌شود.",
    ),
    "sms_messages": dict(
        read=("staff", "physician"),
        write=("system",),
        self_read=(),
        note="متن پیامک‌ها برای بیمار در رسید نمایش داده می‌شود، ولی فهرست نه.",
    ),
    "sms_inbound": dict(
        read=("staff", "physician"),
        write=("system",),
        self_read=(),
    ),
    "patient_sessions": dict(
        read=("staff", "physician"),
        write=("physician",),
        self_read=(),
        note="پورتال بیمار هرگز تاریخچه‌ی درمان را نمی‌خواند.",
    ),
    "session_body_parts": dict(read=("staff", "physician"), write=("physician",), self_read=()),
    "session_treatments": dict(read=("staff", "physician"), write=("physician",), self_read=()),
    "body_parts": dict(read=("staff", "physician"), write=("physician",), self_read=()),
    "treatments": dict(read=("staff", "physician"), write=("physician",), self_read=()),
    "site_requests": dict(read=("staff", "physician"), write=("anonymous",), self_read=()),
    "cost_events": dict(read=("staff", "physician"), write=("system",), self_read=()),
    "rate_events": dict(read=("system",), write=("system",), self_read=()),
}

ROLES = ("anonymous", "patient", "staff", "physician", "system")


def role_of(session_state: dict | None) -> str:
    if not session_state:
        return "anonymous"
    if session_state.get("admin_role"):
        return session_state["admin_role"]
    if session_state.get("patient_id"):
        return "patient"
    return "anonymous"


def authorize(table: str, action: str, role: str) -> tuple[bool, str]:
    policy = POLICIES.get(table)
    if policy is None:
        return False, f"جدول {table} سیاست دسترسی ندارد."
    allowed = policy.get(action if action in ("read", "write") else f"{action}_read", ())
    if allowed is None:
        return False, f"عملیات {action} برای {table} تعریف نشده."
    if role in allowed:
        return True, ""
    return False, f"نقش {role} اجازه‌ی {action} در {table} ندارد."


def visible_fields(table: str, role: str) -> tuple | None:
    """None یعنی همه‌ی ستون‌ها (staff). در غیر این صورت فقط این‌ها."""
    policy = POLICIES[table]
    if role in policy["read"]:
        return None
    if role == "patient":
        return policy.get("self_read", ())
    return ()
