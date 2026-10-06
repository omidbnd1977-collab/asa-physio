"""SMS delivery — every message is stored, costed and retryable.

Providers: `file` (development only), `webhook` (any gateway), `kavenegar`,
`smsir`. Layer 06 applies: each send is checked against the monthly cap *before*
it costs anything and recorded *after* it succeeds.
"""

from __future__ import annotations

import datetime as dt

import httpx

from . import budget, repo, tracking
from .config import settings
from .errors import BudgetExceeded
from .logging_ import fingerprint, info, warn
from .policies import SYSTEM

RESOURCE = "sms"
CLINIC = "آسا فیزیو"


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# message bodies — kept here so the wording is reviewed in one place
# --------------------------------------------------------------------------
def body_registered(name: str) -> str:
    return (
        f"{name} عزیز، اطلاعات شما در {CLINIC} ثبت شد.\n"
        "اکنون می‌توانید نوبت خود را انتخاب کنید.\n"
        "0902464 8159"
    )


def body_booked(name: str, when: str, code: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} ثبت شد.\n"
        f"زمان: {when}\n"
        f"کد پیگیری: {code}\n"
        "جزیره قشم، میدان ولایت، ساختمان محسنین، طبقه دوم"
    )


def body_rescheduled(name: str, old: str, new: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} تغییر کرد.\n"
        f"زمان قبلی: {old}\n"
        f"زمان جدید: {new}\n"
        "در صورت نیاز با ما تماس بگیرید: 0902464 8159"
    )


def body_cancelled(name: str, when: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} برای {when} لغو شد.\n"
        "برای هماهنگی مجدد تماس بگیرید: 0902464 8159"
    )


def body_followup(name: str, when: str) -> str:
    return f"{name} عزیز، جلسه بعدی فیزیوتراپی شما در {CLINIC} ثبت شد.\nزمان: {when}"


def body_welcome_back(name: str, when: str, gap_days: int) -> str:
    months = max(1, round(gap_days / 30))
    gap_fa = "یک ماه" if months == 1 else f"{months} ماه"
    return (
        f"{name} عزیز، از اینکه بعد از {gap_fa} دوباره {CLINIC} را انتخاب کردید "
        "سپاسگزاریم.\n"
        f"نوبت شما: {when}\n"
        "پرونده‌ی قبلی شما نزد ماست و درمان از همان‌جا ادامه پیدا می‌کند."
    )


def body_session_logged(name: str, when: str, treatments: str) -> str:
    return f"{name} عزیز، جلسه‌ی امروز شما در {CLINIC} ثبت شد.\nدرمان انجام‌شده: {treatments}\n{when}"


def body_confirm(name: str, when: str) -> str:
    return (
        f"{name} عزیز، یادآوری نوبت فیزیوتراپی {CLINIC}.\n"
        f"زمان: {when}\n"
        "در صورت حضور قطعی شما در مطب، عدد 1 را ارسال کنید."
    )


def body_custom(name: str, text: str) -> str:
    """A reply the doctor typed in the panel, forwarded to the patient's phone."""
    body = text.strip()[:520]
    return f"{name} عزیز، پیام {CLINIC}:\n{body}"


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
def _send_file(phone: str, body: str) -> None:
    settings.SMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with settings.SMS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"=== {_now()}  ->  {phone}\n{body}\n\n")


def _send_webhook(phone: str, body: str) -> None:
    if not settings.SMS_WEBHOOK:
        raise RuntimeError("SMS_WEBHOOK is empty")
    r = httpx.post(
        settings.SMS_WEBHOOK,
        json={"to": phone, "text": body},
        headers={"Authorization": f"Bearer {settings.SMS_API_KEY}"} if settings.SMS_API_KEY else {},
        timeout=10.0,
    )
    r.raise_for_status()


def _send_kavenegar(phone: str, body: str) -> None:
    if not settings.SMS_API_KEY:
        raise RuntimeError("SMS_API_KEY is empty")
    url = f"https://api.kavenegar.com/v1/{settings.SMS_API_KEY}/sms/send.json"
    r = httpx.post(
        url, data={"receptor": phone, "message": body, "sender": settings.SMS_SENDER}, timeout=10.0
    )
    r.raise_for_status()
    payload = r.json()
    status = payload.get("return", {}).get("status")
    if status != 200:
        raise RuntimeError(f"kavenegar status {status}")


def _send_smsir(phone: str, body: str) -> None:
    if not settings.SMS_API_KEY:
        raise RuntimeError("SMS_API_KEY is empty")
    r = httpx.post(
        "https://api.sms.ir/v1/send/bulk",
        headers={"X-API-KEY": settings.SMS_API_KEY, "Accept": "application/json"},
        json={"lineNumber": settings.SMS_SENDER, "messageText": body, "mobiles": [phone]},
        timeout=10.0,
    )
    r.raise_for_status()


PROVIDERS = {
    "file": _send_file,
    "webhook": _send_webhook,
    "kavenegar": _send_kavenegar,
    "smsir": _send_smsir,
}


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------
def queue(
    phone: str,
    body: str,
    kind: str,
    *,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> int:
    return repo.insert(
        SYSTEM,
        "sms_messages",
        {
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "phone": phone,
            "kind": kind,
            "body": body[:600],
            "provider": settings.SMS_PROVIDER,
        },
    )


def deliver(message_id: int, *, attempts: int = 2) -> bool:
    rows = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[message_id], limit=1)
    if not rows:
        return False
    msg = rows[0]
    if msg["status"] == "sent":
        return True

    send = PROVIDERS.get(settings.SMS_PROVIDER)
    if send is None:
        repo.update(
            SYSTEM,
            "sms_messages",
            {"status": "failed", "last_error": f"unknown provider {settings.SMS_PROVIDER!r}"},
            where="id = ?",
            params=[message_id],
        )
        return False

    # layer 06 — never spend past the monthly cap
    try:
        budget.guard(RESOURCE, 1.0)
    except BudgetExceeded as exc:
        repo.update(
            SYSTEM,
            "sms_messages",
            {"status": "failed", "last_error": str(exc)[:400]},
            where="id = ?",
            params=[message_id],
        )
        tracking.notify(
            "sms_budget_blocked", "an SMS was not sent: cap reached", sms_kind=msg["kind"]
        )
        return False

    last_err = ""
    for i in range(attempts):
        try:
            send(msg["phone"], msg["body"])
            budget.record(RESOURCE, 1.0)
            repo.update(
                SYSTEM,
                "sms_messages",
                {
                    "status": "sent",
                    "sent_at": _now(),
                    "attempts": msg["attempts"] + i + 1,
                    "last_error": "",
                    "provider": settings.SMS_PROVIDER,
                },
                where="id = ?",
                params=[message_id],
            )
            info(
                "sms.sent",
                sms_kind=msg["kind"],
                provider=settings.SMS_PROVIDER,
                to=fingerprint(msg["phone"]),
            )
            return True
        except Exception as exc:  # noqa: BLE001 — gateway failures are expected
            last_err = f"{type(exc).__name__}: {exc}"[:400]
            warn("sms.attempt_failed", kind=msg["kind"], attempt=i + 1, exc_type=type(exc).__name__)

    repo.update(
        SYSTEM,
        "sms_messages",
        {
            "status": "failed",
            "attempts": msg["attempts"] + attempts,
            "last_error": last_err,
        },
        where="id = ?",
        params=[message_id],
    )
    tracking.notify(
        "sms_delivery_failed",
        "an SMS could not be delivered",
        sms_kind=msg["kind"],
        error=last_err[:120],
    )
    return False


def send_now(
    phone: str,
    body: str,
    kind: str,
    *,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> tuple[int, bool]:
    mid = queue(phone, body, kind, patient_id=patient_id, appointment_id=appointment_id)
    return mid, deliver(mid)


def retry_failed(limit: int = 20) -> dict[str, int]:
    rows = repo.select(
        SYSTEM,
        "sms_messages",
        where="status = 'failed' AND attempts < 10",
        order_by="created_at",
        limit=limit,
    )
    ok = sum(1 for r in rows if deliver(r["id"], attempts=1))
    return {"retried": len(rows), "sent": ok, "failed": len(rows) - ok}
