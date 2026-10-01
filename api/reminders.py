"""یادآوری خودکار.

`python3 -m api.reminders` یک‌بار اسکن می‌کند (برای cron); داخل برنامه هم
هر `REMINDER_INTERVAL_SEC` ثانیه اجرا می‌شود.

نکته‌ی مهم: «ادعا» و «علامت‌گذاریِ قبلیِ ارسال» با **یک UPDATE اتمی** انجام
می‌شود، نه خواندن-بعد-نوشتن. وقتی درون‌برنامه‌ای هر ۶۰ ثانیه و cron هر ۵ دقیقه
هم‌زمان بدوند، همین `WHERE confirm_sent_at IS NULL` است که جلوی پیامک تکراری
را می‌گیرد. اگر ارسال شکست بخورد، ستون پاک می‌شود تا تلاشِ بعدی ممکن باشد
(با سقف تلاش).
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from . import db as dbm
from . import sms as smsm
from .config import settings
from .jalali import jalali_of, to_persian_digits

log = logging.getLogger("asa.reminders")

MAX_ATTEMPTS = 3


def _today_jalali() -> str:
    from .appointments import jdate_today

    return jdate_today()


def _as_local(dt_text: str) -> datetime:
    dt = datetime.fromisoformat(dt_text)
    return dt if dt.tzinfo else dt.replace(tzinfo=settings.tz())


def claim(conn, appointment_id: int) -> bool:
    """compare-and-set: فقط کسی که ردیف را تغییر می‌دهد ارسال می‌کند."""
    cur = conn.execute(
        "UPDATE appointments SET confirm_sent_at = ?, reminder_attempts = reminder_attempts + 1"
        " WHERE id = ? AND confirm_sent_at IS NULL AND status IN ('booked','coming')",
        (dbm.now_iso(), appointment_id),
    )
    return cur.rowcount == 1


def release(conn, appointment_id: int) -> None:
    """ارسال نشد ⇒ علامت را برمی‌داریم تا تلاشِ بعدی باشد."""
    conn.execute("UPDATE appointments SET confirm_sent_at = NULL WHERE id = ?"
                 " AND reminder_attempts < ?", (appointment_id, MAX_ATTEMPTS))


def due_rows(conn, now: datetime):
    lead_end = now + timedelta(minutes=settings.REMINDER_LEAD_MIN)
    rows = conn.execute(
        "SELECT a.*, p.full_name, p.mobile, p.id AS pid FROM appointments a"
        " JOIN patients p ON p.id = a.patient_id"
        " WHERE a.status IN ('booked','coming') AND a.confirm_sent_at IS NULL"
        " AND a.reminder_attempts < ?"
        " AND a.slot_date >= ?",          # slot_date شمسی است؛ باید با امروزِ شمسی سنجیده شود
        (MAX_ATTEMPTS, _today_jalali()),
    ).fetchall()
    due = []
    for r in rows:
        start = _slot_start(r["slot_date"], r["slot_time"])
        if start is None:
            continue
        if now <= start <= lead_end:
            due.append(r)
    return due


def _slot_start(jdate: str, hhmm: str) -> datetime | None:
    from .appointments import gregorian_of

    try:
        return datetime.combine(gregorian_of(jdate), datetime.strptime(hhmm, "%H:%M").time(),
                                tzinfo=settings.tz())
    except (ValueError, TypeError):
        return None


last_scan: dict = {"sent": 0, "skipped": 0, "source": "never", "at": "-"}


def scan_once(source: str = "loop") -> dict:
    """یک دور اسکن + ارسال. برمی‌گرداند شمارش (برای لاگ و تب سامانه)."""
    conn = dbm.db()
    now = settings.now()
    sent = skipped = 0
    for row in due_rows(conn, now):
        with dbm.tx(immediate=True):
            if not claim(conn, row["id"]):
                skipped += 1            # رقیب (cron/worker دیگر) برداشته است
                continue
            res = smsm.send(
                conn, event="confirm", to=row["mobile"], patient_id=row["pid"],
                appointment_id=row["id"],
                body=smsm.render("confirm", name=row["full_name"],
                                 jdate=jalali_of(_start_date(row))["long"],
                                 time=to_persian_digits(row["slot_time"])),
            )
            if res["status"] == "sent":
                sent += 1
            else:
                release(conn, row["id"])
                skipped += 1
                log.warning("reminder %s not sent (%s), retry scheduled",
                            row["tracking_code"], res["status"])
    global last_scan
    out = {"sent": sent, "skipped": skipped, "source": source,
           "at": now.strftime("%H:%M:%S")}
    last_scan = out
    if sent or skipped:
        print(f"[reminders:{source}] sent={sent} skipped={skipped} at={out['at']}", flush=True)
    return out


def _start_date(row) -> "date":  # noqa: F821
    from .appointments import gregorian_of

    return gregorian_of(row["slot_date"])


_stop = threading.Event()


def start_in_app_thread() -> threading.Thread | None:
    """هر ۶۰ ثانیه داخل خود برنامه — همان چیزی که BOOKING.md توصیف می‌کند."""
    def loop():
        dbm.init()
        while not _stop.wait(settings.REMINDER_INTERVAL_SEC):
            try:
                scan_once("in-app")
            except Exception as exc:  # هرگز نخِ برنامه را نکش
                log.warning("reminder loop error: %s", type(exc).__name__)

    th = threading.Thread(target=loop, name="asa-reminders", daemon=True)
    th.start()
    return th


def stop() -> None:
    _stop.set()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dbm.init()
    print(scan_once("cli"))
