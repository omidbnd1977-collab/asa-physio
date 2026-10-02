"""Clinic hours, the slot grid, and race-safe cabin allocation.

Opening hours: 16:00 to 22:00 Tehran time, one appointment every 30 minutes,
10 cabins running in parallel.

    12 start times (16:00 … 21:30) x 10 cabins = 120 appointments per day.

A slot is "full" when all 10 cabins are taken for that start time. Allocation happens
inside `BEGIN IMMEDIATE`; the `UNIQUE(slot_date, slot_time, cabin)` constraint in
migration 0003 is the real guarantee, so two simultaneous requests can never land on
the same cabin.
"""

from __future__ import annotations

import datetime as dt
import secrets

from . import repo
from .errors import BadRequest, Conflict
from .jalali import TEHRAN, now_tehran, to_jalali_long, to_jalali_str
from .policies import SYSTEM

OPEN_HOUR = 16
CLOSE_HOUR = 22
STEP_MIN = 30
CABINS = 10
BOOKING_HORIZON_DAYS = 30
# a patient cannot book a slot that starts in less than this many minutes
MIN_LEAD_MIN = 60


def slot_times() -> list[str]:
    out: list[str] = []
    t = dt.datetime(2000, 1, 1, OPEN_HOUR, 0)
    end = dt.datetime(2000, 1, 1, CLOSE_HOUR, 0)
    while t + dt.timedelta(minutes=STEP_MIN) <= end:
        out.append(t.strftime("%H:%M"))
        t += dt.timedelta(minutes=STEP_MIN)
    return out


SLOTS = slot_times()
DAY_CAPACITY = len(SLOTS) * CABINS


def slot_datetime(slot_date: str, slot_time: str) -> dt.datetime:
    h, m = (int(x) for x in slot_time.split(":"))
    d = dt.date.fromisoformat(slot_date)
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=TEHRAN)


def open_days() -> list[dict[str, str | bool]]:
    """The dates a patient may pick, starting today (Tehran)."""
    today = now_tehran().date()
    out = []
    for i in range(BOOKING_HORIZON_DAYS):
        d = today + dt.timedelta(days=i)
        iso = d.isoformat()
        out.append(
            {
                "date": iso,
                "jalali": to_jalali_str(d),
                "label": to_jalali_long(d),
                "is_today": i == 0,
            }
        )
    return out


def validate_slot(slot_date: str, slot_time: str) -> dt.datetime:
    if slot_time not in SLOTS:
        raise BadRequest(
            "ساعت انتخابی خارج از ساعات کاری کلینیک است (۱۶:۰۰ تا ۲۲:۰۰).",
            fields={"slot_time": "ساعت نامعتبر"},
        )
    try:
        d = dt.date.fromisoformat(slot_date)
    except ValueError as exc:
        raise BadRequest("تاریخ انتخابی معتبر نیست.", fields={"slot_date": "نامعتبر"}) from exc
    today = now_tehran().date()
    if d < today:
        raise BadRequest("نمی‌توان برای گذشته نوبت گرفت.", fields={"slot_date": "گذشته"})
    if (d - today).days >= BOOKING_HORIZON_DAYS:
        raise BadRequest(
            f"فعلاً فقط تا {BOOKING_HORIZON_DAYS} روز آینده می‌توانید نوبت بگیرید.",
            fields={"slot_date": "خارج از بازه"},
        )
    when = slot_datetime(slot_date, slot_time)
    if when - now_tehran() < dt.timedelta(minutes=MIN_LEAD_MIN):
        raise BadRequest(
            "این ساعت خیلی نزدیک است. لطفاً ساعت دیرتری انتخاب کنید یا تلفنی تماس بگیرید.",
            fields={"slot_time": "دیر است"},
        )
    return when


def taken_map(slot_date: str) -> dict[str, int]:
    rows = repo.select(
        SYSTEM,
        "appointments",
        columns=["slot_time"],
        where="slot_date = ? AND status != 'cancelled'",
        params=[slot_date],
    )
    out: dict[str, int] = {}
    for r in rows:
        out[r["slot_time"]] = out.get(r["slot_time"], 0) + 1
    return out


def day_grid(slot_date: str, *, for_patient: bool = True) -> dict[str, object]:
    """What the patient's table shows. Full or past slots come back disabled."""
    taken = taken_map(slot_date)
    now = now_tehran()
    d = dt.date.fromisoformat(slot_date)
    rows = []
    for t in SLOTS:
        used = taken.get(t, 0)
        free = CABINS - used
        when = slot_datetime(slot_date, t)
        too_soon = for_patient and (when - now) < dt.timedelta(minutes=MIN_LEAD_MIN)
        rows.append(
            {
                "time": t,
                "free": free,
                "capacity": CABINS,
                "full": free <= 0,
                "past": too_soon,
                "disabled": free <= 0 or too_soon,
            }
        )
    return {
        "date": slot_date,
        "jalali": to_jalali_str(d),
        "label": to_jalali_long(d),
        "capacity": DAY_CAPACITY,
        "booked": sum(taken.values()),
        "slots": rows,
    }


def allocate(
    patient_id: int,
    slot_date: str,
    slot_time: str,
    *,
    kind: str = "first",
    booked_by: str = "patient",
    note: str = "",
    moved_from: str = "",
) -> dict[str, object]:
    """Reserve the lowest free cabin. Raises Conflict when the slot just filled up."""
    validate_slot(slot_date, slot_time)
    public_id = secrets.token_hex(5)
    with repo.tx() as conn:
        rows = conn.execute(
            "SELECT cabin FROM appointments "
            "WHERE slot_date = ? AND slot_time = ? AND status != 'cancelled'",
            (slot_date, slot_time),
        ).fetchall()
        used = {r["cabin"] for r in rows}
        free = next((c for c in range(1, CABINS + 1) if c not in used), None)
        if free is None:
            raise Conflict(
                "این ساعت همین الان پر شد. لطفاً ساعت دیگری انتخاب کنید.",
                code="slot_full",
            )
        dup = conn.execute(
            "SELECT 1 FROM appointments WHERE patient_id = ? AND slot_date = ? "
            "AND slot_time = ? AND status != 'cancelled'",
            (patient_id, slot_date, slot_time),
        ).fetchone()
        if dup:
            raise Conflict("برای همین ساعت قبلاً نوبت دارید.", code="duplicate_booking")
        cur = conn.execute(
            "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin,"
            " kind, booked_by, note, moved_from) VALUES (?,?,?,?,?,?,?,?,?)",
            (public_id, patient_id, slot_date, slot_time, free, kind, booked_by, note, moved_from),
        )
        appt_id = int(cur.lastrowid or 0)
    return {
        "id": appt_id,
        "public_id": public_id,
        "cabin": free,
        "slot_date": slot_date,
        "slot_time": slot_time,
        "kind": kind,
    }


def describe(appt: dict) -> str:
    return f"{to_jalali_long(appt['slot_date'])} ساعت {appt['slot_time']}"
