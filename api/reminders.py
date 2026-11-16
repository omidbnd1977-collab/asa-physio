"""The confirmation SMS that goes out two hours before an appointment, and the
inbound reply that marks the patient as attending.

Runs as a background task inside the app and is also callable from cron
(`python -m api.reminders`) so a restart can never silently stop reminders.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sqlite3

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
        # claim first: only the winner of the UPDATE may send (crash- and race-safe)
        claimed = repo.update(
            SYSTEM,
            "appointments",
            {"confirm_sent_at": _now_utc()},
            where="id = ? AND confirm_sent_at IS NULL",
            params=[a["id"]],
        )
        if claimed == 0:
            continue
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
def _normalize_inbound_phone(phone: str) -> str:
    """Map SMS.ir ``912…`` / ``+98…`` forms onto stored ``09…`` patient phones."""
    return sms.normalize_mobile(phone.strip())


def _decision_from_body(body: str) -> str | None:
    text = body.strip().lower()
    first = text.split()[0] if text.split() else ""
    if first in CONFIRM_WORDS or text in CONFIRM_WORDS:
        return "coming"
    if first in DECLINE_WORDS or text in DECLINE_WORDS:
        return "not_coming"
    return None


def _duplicate_response(row: dict) -> dict[str, object]:
    public_id = None
    appt_id = row.get("appointment_id")
    if appt_id:
        found = repo.select(SYSTEM, "appointments", where="id = ?", params=[appt_id], limit=1)
        if found:
            public_id = found[0].get("public_id")
    return {
        "matched": bool(appt_id),
        "handled": row.get("handled") or "ignored",
        "appointment": public_id,
        "duplicate": True,
    }


def _find_confirm_target(patient_id: int) -> dict | None:
    """Nearest upcoming booked appointment that already received confirm_request."""
    now = now_tehran()
    appts = repo.select(
        SYSTEM,
        "appointments",
        where=(
            "patient_id = ? AND status = 'booked' AND confirm_sent_at IS NOT NULL "
            "AND slot_date >= ?"
        ),
        params=[patient_id, now.date().isoformat()],
    )
    candidates = [
        a
        for a in appts
        if scheduling.slot_datetime(a["slot_date"], a["slot_time"]) >= now - dt.timedelta(hours=1)
    ]
    # Nearest slot first; among ties prefer the newest confirm_sent_at (stable sort).
    candidates.sort(key=lambda a: a.get("confirm_sent_at") or "", reverse=True)
    candidates.sort(key=lambda a: (a["slot_date"], a["slot_time"]))
    return candidates[0] if candidates else None


def handle_inbound(
    phone: str, body: str, *, provider_msg_id: str | None = None
) -> dict[str, object]:
    """A patient texted back. '1' confirms attendance; '2' declines.

    Only appointments with ``confirm_sent_at`` (confirm_request already sent) are
    eligible. Optional ``provider_msg_id`` (SMS.ir ReceiveReturnId) makes processing
    idempotent across divert retries.
    """
    phone = _normalize_inbound_phone(phone)
    pmid = (provider_msg_id or "").strip() or None
    if pmid:
        existing = repo.select(
            SYSTEM, "sms_inbound", where="provider_msg_id = ?", params=[pmid], limit=1
        )
        if existing:
            return _duplicate_response(existing[0])

    decision = _decision_from_body(body)

    rows = repo.select(SYSTEM, "patients", where="phone = ?", params=[phone], limit=1)
    if not rows:
        try:
            repo.insert(
                SYSTEM,
                "sms_inbound",
                {
                    "phone": phone,
                    "body": body[:300],
                    "handled": "unknown_phone",
                    "patient_id": None,
                    "owner_id": None,
                    "provider_msg_id": pmid,
                },
            )
        except sqlite3.IntegrityError:
            if pmid:
                again = repo.select(
                    SYSTEM, "sms_inbound", where="provider_msg_id = ?", params=[pmid], limit=1
                )
                if again:
                    return _duplicate_response(again[0])
            raise
        warn("sms.inbound_unknown_phone", phone=fingerprint(phone))
        return {"matched": False, "reason": "unknown_phone", "duplicate": False}

    patient = rows[0]
    patient_owner = patient.get("owner_id")
    target = _find_confirm_target(int(patient["id"]))

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

    try:
        repo.insert(
            SYSTEM,
            "sms_inbound",
            {
                "phone": phone,
                "body": body[:300],
                "appointment_id": target["id"] if target else None,
                "handled": handled,
                "patient_id": patient["id"],
                "owner_id": patient_owner,
                "provider_msg_id": pmid,
            },
        )
    except sqlite3.IntegrityError:
        if pmid:
            again = repo.select(
                SYSTEM, "sms_inbound", where="provider_msg_id = ?", params=[pmid], limit=1
            )
            if again:
                return _duplicate_response(again[0])
        raise

    info("sms.inbound", handled=handled, has_appointment=bool(target), duplicate=False)
    return {
        "matched": bool(target),
        "handled": handled,
        "appointment": target["public_id"] if target else None,
        "duplicate": False,
    }


if __name__ == "__main__":  # python -m api.reminders  (for cron)
    from .db import migrate
    from .logging_ import setup_logging

    setup_logging()
    migrate()
    result = run_once()
    print(f"reminders: due={result['due']} sent={result['sent']} failed={result['failed']}")
    _ = TEHRAN
