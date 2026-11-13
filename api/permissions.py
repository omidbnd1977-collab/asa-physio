"""Granular staff (secretary) capabilities — enforced in the API, not only the UI."""

from __future__ import annotations

from typing import Final

# Stable keys stored in staff_permissions.permission
PATIENTS_VIEW: Final = "patients.view"
PATIENTS_CREATE: Final = "patients.create"
PATIENTS_EDIT: Final = "patients.edit"
APPOINTMENTS_VIEW: Final = "appointments.view"
APPOINTMENTS_CREATE: Final = "appointments.create"
APPOINTMENTS_MOVE: Final = "appointments.move"
APPOINTMENTS_CANCEL: Final = "appointments.cancel"
MESSAGES_VIEW: Final = "messages.view"
RECEPTION_QUEUE: Final = "reception.queue"
CLINICAL_RECORD: Final = "clinical.record"
FILES_MANAGE: Final = "files.manage"

ALL: Final[tuple[str, ...]] = (
    PATIENTS_VIEW,
    PATIENTS_CREATE,
    PATIENTS_EDIT,
    APPOINTMENTS_VIEW,
    APPOINTMENTS_CREATE,
    APPOINTMENTS_MOVE,
    APPOINTMENTS_CANCEL,
    MESSAGES_VIEW,
    RECEPTION_QUEUE,
    CLINICAL_RECORD,
    FILES_MANAGE,
)

LABELS_FA: Final[dict[str, str]] = {
    PATIENTS_VIEW: "مشاهده فهرست بیماران",
    PATIENTS_CREATE: "ثبت بیمار جدید",
    PATIENTS_EDIT: "ویرایش اطلاعات بیمار",
    APPOINTMENTS_VIEW: "مشاهده نوبت‌ها",
    APPOINTMENTS_CREATE: "ثبت نوبت جدید",
    APPOINTMENTS_MOVE: "جابه‌جایی نوبت",
    APPOINTMENTS_CANCEL: "لغو نوبت",
    MESSAGES_VIEW: "مشاهده پیام‌ها و درخواست‌های مشاورهٔ تخصیص‌شده",
    RECEPTION_QUEUE: "صف پذیرش (پیامک و مشاورهٔ بدون مالک)",
    CLINICAL_RECORD: "ثبت جلسه درمانی و اطلاعات بالینی",
    FILES_MANAGE: "مشاهده و مدیریت تصاویر و فایل‌های MRI",
}


def normalize(perms: list[str] | tuple[str, ...] | set[str] | None) -> frozenset[str]:
    if not perms:
        return frozenset()
    allowed = set(ALL)
    return frozenset(p for p in perms if p in allowed)
