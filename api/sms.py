"""پیامک‌ها: قالب‌ها، کانال‌ها، و سقف ماهانه.

`SMS_PROVIDER=file` فقط برای توسعه است؛ در `APP_ENV=production` برنامه با آن
بالا نمی‌آید — همان‌طور که BOOKING.md نوشته. هر پیامک پیش از ارسال با سقف
ماهانه بررسی و پس از ارسال در `cost_events` ثبت می‌شود.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

from .config import settings
from .security import latinize_digits

log = logging.getLogger("asa.sms")

# اعداد در متن: لاتین می‌مانند تا «۱» و «1» هر دو کار کنند، و پیام‌های
# «شما عدد 1 را بفرستید» با پاسخ فارسی هم مطابقت کنند.
TEMPLATES = {
    "registered": "{name} عزیز، اطلاعات شما در {clinic} ثبت شد.\n"
                  "برای رزرو نوبت وارد جدول نوبت‌ها شوید.",
    "booked": "{name} عزیز، نوبت شما ثبت شد.\n{jdate} — ساعت {time} — کابین {cabin}\n"
              "کد پیگیری: {code}\n{address}\nدر صورت تغییر برنامه لطفاً تماس بگیرید: {phone}",
    "rescheduled": "{name} عزیز، نوبت شما تغییر کرد.\n"
                   "قبلی: {prev_jdate} ساعت {prev_time}\n"
                   "جدید: {jdate} ساعت {time} — کابین {cabin}\nکد پیگیری: {code}",
    "cancelled": "{name} عزیز، نوبت {jdate} ساعت {time} لغو شد.\n"
                  "برای رزرو دوباره با {phone} تماس بگیرید.",
    "followup": "{name} عزیز، جلسه‌ی بعدی شما: {jdate} ساعت {time} — کابین {cabin}\n"
                "کد پیگیری: {code}",
    "confirm": "{name} عزیز، {jdate} ساعت {time} منتظرتان هستیم.\n"
               "در صورت حضور قطعی شما در مطب، عدد 1 را ارسال کنید.\n"
               "غیرممکن است؟ عدد 2.",
    "welcome_back": "{name} عزیز، از اینکه بعد از {gap} دوباره {clinic} را انتخاب کردید "
                    "سپاسگزاریم.\nنوبت شما: {jdate} ساعت {time}\n"
                    "پرونده‌ی قبلی شما نزد ماست و درمان از همان‌جا ادامه پیدا می‌کند.",
    "session_summary": "{name} عزیز، جلسه‌ی {jdate}: {treatments}. "
                       "درد شما از {pain_before} به {pain_after} رسید.",
    "session_summary_nopain": "{name} عزیز، جلسه‌ی {jdate}: {treatments}.",
}

EVENT_LABELS = {
    "registered": "ثبت‌نام",
    "booked": "ثبت نوبت",
    "rescheduled": "تغییر نوبت",
    "cancelled": "لغو نوبت",
    "followup": "جلسه‌ی بعدی",
    "confirm": "یادآوری (تأیید حضور)",
    "welcome_back": "بازگشت پس از وقفه",
    "session_summary": "خلاصه‌ی جلسه",
    "welcome_back+booked": "بازگشت + ثبت نوبت",
}


class ProviderError(RuntimeError):
    pass


def assert_provider() -> None:
    if settings.SMS_PROVIDER not in {"kavenegar", "smsir", "webhook", "file"}:
        raise SystemExit(f"SMS_PROVIDER ناشناخته: {settings.SMS_PROVIDER}")
    if settings.APP_ENV == "production" and settings.SMS_PROVIDER == "file":
        raise SystemExit(
            "SMS_PROVIDER=file در production مجاز نیست — تا هرگز به بیماری "
            "گفته نشود پیامکی رفته که نرفته."
        )


def render(event: str, **ctx) -> str:
    template = TEMPLATES[event]
    return template.format(clinic=settings.CLINIC_NAME, address=settings.ADDRESS,
                           phone=settings.PHONE, **ctx)


def _http(url: str, data: dict | None = None, headers: dict | None = None,
          timeout: int = 8) -> str:
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - ثابت/پیکربندی
        return resp.read().decode("utf-8", "replace")


def _send_kavenegar(to: str, text: str) -> str:
    if not settings.KAVENEGAR_KEY:
        raise ProviderError("KAVENEGAR_KEY تنظیم نشده")
    local = "0" + to[4:] if to.startswith("0098") else to
    qs = urllib.parse.urlencode({"receiver": local, "message": text, "template": ""})
    raw = _http(f"https://api.kavenegar.com/v1/{settings.KAVENEGAR_KEY}/sms/send.json?{qs}")
    try:
        payload = json.loads(raw)
        if int(payload.get("returnCode", 0)) >= 400:
            raise ProviderError(f"kavenegar: {payload.get('errorMessage') or raw[:120]}")
    except json.JSONDecodeError:
        pass
    return raw[:120]


def _send_smsir(to: str, text: str) -> str:
    if not settings.SMSIR_KEY:
        raise ProviderError("SMSIR_KEY تنظیم نشده")
    url = "https://ippanel.com/api/select"
    body = json.dumps({
        "inputNumber": [to], "message": [text], "outputNumber": settings.SMSIR_LINE,
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {settings.SMSIR_KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec
        return resp.read().decode("utf-8", "replace")[:120]


def _send_webhook(to: str, text: str) -> str:
    if not settings.SMS_WEBHOOK_URL:
        raise ProviderError("SMS_WEBHOOK_URL تنظیم نشده")
    body = json.dumps({"phone": to, "body": text, "line": settings.CLINIC_NAME}).encode()
    req = urllib.request.Request(settings.SMS_WEBHOOK_URL, data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:  # nosec
        return resp.read().decode("utf-8", "replace")[:120]


def _send_file(to: str, text: str) -> str:
    os.makedirs(os.path.dirname(str(settings.SMS_LOG_PATH)), exist_ok=True)
    stamp = settings.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(str(settings.SMS_LOG_PATH), "a", encoding="utf-8") as fh:
        fh.write(f"[{stamp}] -> {to}\n{text}\n{'-' * 60}\n")
    return f"file:{stamp}"


PROVIDERS = {
    "kavenegar": _send_kavenegar,
    "smsir": _send_smsir,
    "webhook": _send_webhook,
    "file": _send_file,
}


def dispatch(to: str, text: str) -> tuple[bool, str, str]:
    """(ok, provider_id, error)"""
    fn = PROVIDERS[settings.SMS_PROVIDER]
    try:
        pid = fn(to, text)
        return True, pid, ""
    except ProviderError as exc:
        return False, "", str(exc)
    except Exception as exc:  # شبکه/پروایدر
        log.warning("sms dispatch failed: %s", type(exc).__name__)
        return False, "", f"{type(exc).__name__}: {exc}"[:200]


def send(conn, *, event: str, to: str, body: str, patient_id=None, appointment_id=None,
         allow_over_budget: bool = False) -> dict:
    """ثبت + ارسال با نگهبانِ سقف. برمی‌گرداد رکورد پیامک."""
    from . import db as dbm
    from .security import fingerprint

    now = dbm.now_iso()
    projected = dbm.month_cost_usd(conn) + settings.SMS_UNIT_COST_USD
    if projected > settings.SMS_BUDGET_MONTHLY_USD and not allow_over_budget:
        cur = conn.execute(
            "INSERT INTO sms_messages(patient_id, appointment_id, event, to_phone, body,"
            " status, error, cost_usd, created_at) VALUES(?,?,?,?,?,'skipped_budget',"
            " 'budget_exceeded_before_send',0,?)",
            (patient_id, appointment_id, event, to, body, now),
        )
        conn.execute(
            "UPDATE sms_messages SET error = ? WHERE id = ?",
            (f"سقف ماهانه‌ی {settings.SMS_BUDGET_MONTHLY_USD}$ پر شده بود؛ ارسال نشد.",
             cur.lastrowid),
        )
        return {"id": cur.lastrowid, "status": "skipped_budget", "provider_id": "",
                "error": "budget", "to": to, "body": body, "event": event}

    ok, pid, err = dispatch(to, body)
    cost = settings.SMS_UNIT_COST_USD if ok else 0.0
    conn.execute(
        "INSERT INTO sms_messages(patient_id, appointment_id, event, to_phone, body,"
        " status, error, provider_id, cost_usd, created_at, sent_at) VALUES("
        "?,?,?,?,?,?,?,?,?,?,?)",
        (patient_id, appointment_id, event, to, body,
         "sent" if ok else "failed", err or None, pid or None, cost, now,
         now if ok else None),
    )
    sms_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    if ok:
        conn.execute(
            "INSERT INTO cost_events(kind, amount_usd, ref, created_at) VALUES('sms',?,?,?)",
            (cost, f"sms:{sms_id}:{fingerprint(to)}", now),
        )
    else:
        log.warning("sms failed (%s) → phone %s", event, fingerprint(to))
    return {"id": sms_id, "status": "sent" if ok else "failed", "provider_id": pid,
            "error": err, "to": to, "body": body, "event": event}


def looks_yes(body: str) -> bool:
    b = latinize_digits(body or "").strip().lower()
    return b in {"1", "yes", "y", "ok", "بله", "ب", "تایید", "تأیید", "حضور دارم", "میام"}


def looks_no(body: str) -> bool:
    b = latinize_digits(body or "").strip().lower()
    return b in {"2", "no", "n", "نه", "ن", "لغو", "نمیام", "مشکل دارم"}
