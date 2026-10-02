"""The confirmation SMS that goes out two hours before an appointment, and the
inbound reply that marks the patient as attending.

Runs as a background task inside the app and is also callable from cron
(`python -m api.reminders`) so a restart can never silently stop reminders.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from . import cache, repo, scheduling, sms
from .config import settings
from .jalali import TEHRAN, now_tehran
from .logging_ import fingerprint, info, warn
from .policies import SYSTEM

CONFIRM_WORDS = {"1", "١", "۱", "بله", "اره", "آره", "ok", "okay", "yes", "y", "تایید", "تأیید"}
DECLINE_WORDS = {"2", "٢", "۲", "خیر", "نه", "no", "n", "لغو", "cancel"}


def _now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def due_appointments(lead_min: int | None = None) -> list[dict]:
    """Booked appointments starting within the lead window that have not been asked yet."""
    lead = lead_min if lead_min is not None else settings.REMINDER_LEAD_MIN
    now = now_tehran()
    horizon = now + dt.timedelta(minutes=lead)
    rows = repo.select(
        SYSTEM,
        "appointments",
        where="status = 'booked' AND confirm_sent_at IS NULL AND slot_date BETWEEN ? AND ?",
        params=[now.date().isoformat(), horizon.date().isoformat()],
    )
    due = []
    for a in rows:
        when = scheduling.slot_datetime(a["slot_date"], a["slot_time"])
        if now < when <= horizon:
            due.append(a)
    return due


def run_once(lead_min: int | None = None) -> dict[str, int]:
    """Send every confirmation request that is due. Safe to call repeatedly."""
    due = due_appointments(lead_min)
    sent = failed = 0
    for a in due:
        p = repo.select(SYSTEM, "patients", where="id = ?", params=[a["patient_id"]], limit=1)
        if not p:
            continue
        patient = p[0]
        # mark first: a crash must never produce a second text to the same patient
        repo.update(
            SYSTEM,
            "appointments",
            {"confirm_sent_at": _now_utc()},
            where="id = ? AND confirm_sent_at IS NULL",
            params=[a["id"]],
        )
        _, ok = sms.send_now(
            patient["phone"],
            sms.body_confirm(patient["full_name"], scheduling.describe(a)),
            "confirm_request",
            patient_id=patient["id"],
            appointment_id=a["id"],
        )
        if ok:
            sent += 1
        else:
            failed += 1
    if due:
        cache.purge("admin:appointments")
        info("reminder.tick", due=len(due), sent=sent, failed=failed)
    return {"due": len(due), "sent": sent, "failed": failed}


async def loop() -> None:
    """Background ticker started by the app lifespan."""
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the ticker must never die
            from . import tracking
            from .logging_ import new_trace_id

            tracking.capture(exc, trace_id=new_trace_id(), where="reminders.loop")
        await asyncio.sleep(settings.REMINDER_TICK_S)


# --------------------------------------------------------------------------
# inbound reply
# --------------------------------------------------------------------------
def handle_inbound(phone: str, body: str) -> dict[str, object]:
    """A patient texted back. '1' means they are definitely coming."""
    text = body.strip().lower()
    first = text.split()[0] if text.split() else ""
    if first in CONFIRM_WORDS or text in CONFIRM_WORDS:
        decision = "coming"
    elif first in DECLINE_WORDS or text in DECLINE_WORDS:
        decision = "not_coming"
    else:
        decision = None

    rows = repo.select(SYSTEM, "patients", where="phone = ?", params=[phone], limit=1)
    if not rows:
        repo.insert(
            SYSTEM, "sms_inbound", {"phone": phone, "body": body[:300], "handled": "unknown_phone"}
        )
        warn("sms.inbound_unknown_phone", phone=fingerprint(phone))
        return {"matched": False, "reason": "unknown_phone"}
    patient = rows[0]

    now = now_tehran()
    appts = repo.select(
        SYSTEM,
        "appointments",
        where="patient_id = ? AND status = 'booked' AND slot_date >= ?",
        params=[patient["id"], now.date().isoformat()],
    )
    upcoming = sorted(
        (
            a
            for a in appts
            if scheduling.slot_datetime(a["slot_date"], a["slot_time"])
            >= now - dt.timedelta(hours=1)
        ),
        key=lambda a: (a["slot_date"], a["slot_time"]),
    )
    target = upcoming[0] if upcoming else None

    handled = "ignored"
    if target and decision:
        repo.update(
            SYSTEM,
            "appointments",
            {
                "attendance": decision,
                "confirmed_at": _now_utc() if decision == "coming" else None,
                "updated_at": _now_utc(),
            },
            where="id = ?",
            params=[target["id"]],
        )
        handled = "confirmed" if decision == "coming" else "declined"
        if decision == "coming":
            from . import metrics

            metrics.record("attendance_confirmed")
        cache.purge("admin:appointments")

    repo.insert(
        SYSTEM,
        "sms_inbound",
        {
            "phone": phone,
            "body": body[:300],
            "appointment_id": target["id"] if target else None,
            "handled": handled,
        },
    )
    info("sms.inbound", handled=handled, has_appointment=bool(target))
    return {
        "matched": bool(target),
        "handled": handled,
        "appointment": target["public_id"] if target else None,
    }


if __name__ == "__main__":  # python -m api.reminders  (for cron)
    from .db import migrate
    from .logging_ import setup_logging

    setup_logging()
    migrate()
    result = run_once()
    print(f"reminders: due={result['due']} sent={result['sent']} failed={result['failed']}")
    _ = TEHRAN
