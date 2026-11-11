"""Clinic hours, the slot grid, and race-safe cabin allocation.

Clinic window: 16:00–22:00 Tehran time. Each appointment lasts 90 minutes,
with start times at 16:00, 17:30, 19:00, and 20:30 across 10 cabins.

A slot is "full" when all 10 cabins are taken for that start time. Allocation happens
inside `BEGIN IMMEDIATE`; the partial unique index `idx_appt_active_cabin`
(migration 0009: UNIQUE on slot/cabin WHERE status != 'cancelled') is the real
guarantee, so two active bookings can never share a cabin while cancelled rows keep
their history and free the cabin for reuse.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3

from . import repo
from .errors import BadRequest, Conflict
from .jalali import TEHRAN, now_tehran, to_jalali_long, to_jalali_str
from .policies import SYSTEM


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

OPEN_HOUR = 16
CLOSE_HOUR = 22
STEP_MIN = 90
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


def closed_days_map() -> dict[str, str]:
    """Current closure state derived from immutable audit events (no schema change)."""
    rows = repo.select(
        SYSTEM,
        "audit_logs",
        columns=["action", "entity_id", "after_json"],
        where="entity = ? AND action IN (?, ?)",
        params=["clinic_closed_day", "clinic.day_closed", "clinic.day_reopened"],
        order_by="id desc",
        limit=5000,
    )
    closed: dict[str, str] = {}
    seen: set[str] = set()
    for row in rows:
        day = row["entity_id"]
        if day in seen:
            continue
        seen.add(day)
        if row["action"] == "clinic.day_closed":
            try:
                reason = json.loads(row["after_json"]).get("reason", "")
            except (TypeError, ValueError):
                reason = ""
            closed[day] = reason if isinstance(reason, str) else ""
    return closed


def open_days() -> list[dict[str, str | bool]]:
    """The dates a patient may pick, starting today (Tehran)."""
    today = now_tehran().date()
    closed = closed_days_map()
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
                "closed": iso in closed,
                "closed_reason": closed.get(iso, ""),
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
    if is_closed_day(slot_date):
        raise Conflict(
            "کلینیک در این روز تعطیل است و امکان ثبت نوبت وجود ندارد.",
            code="day_closed",
        )
    when = slot_datetime(slot_date, slot_time)
    if when - now_tehran() < dt.timedelta(minutes=MIN_LEAD_MIN):
        raise BadRequest(
            "این ساعت خیلی نزدیک است. لطفاً ساعت دیرتری انتخاب کنید یا تلفنی تماس بگیرید.",
            fields={"slot_time": "دیر است"},
        )
    return when


def is_closed_day(slot_date: str) -> bool:
    """Return whether staff have explicitly closed the clinic for this date."""
    return slot_date in closed_days_map()


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
    closures = closed_days_map()
    closed = slot_date in closures
    closed_reason = closures.get(slot_date, "")
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
                "disabled": closed or free <= 0 or too_soon,
            }
        )
    return {
        "date": slot_date,
        "jalali": to_jalali_str(d),
        "label": to_jalali_long(d),
        "capacity": DAY_CAPACITY,
        "booked": sum(taken.values()),
        "closed": closed,
        "closed_reason": closed_reason,
        "slots": rows,
    }


def allocate_on_conn(
    conn: sqlite3.Connection,
    patient_id: int,
    slot_date: str,
    slot_time: str,
    *,
    kind: str = "first",
    booked_by: str = "patient",
    note: str = "",
    moved_from: str = "",
) -> dict[str, object]:
    """Insert into an already-open transaction. Does not BEGIN/COMMIT.

    Caller must hold `BEGIN IMMEDIATE` (e.g. via `repo.tx`) so cancel+insert
    in `reschedule` stay atomic. Concurrent UNIQUE violations become Conflict.
    """
    if is_closed_day(slot_date):
        raise Conflict(
            "کلینیک در این روز تعطیل است و امکان ثبت نوبت وجود ندارد.",
            code="day_closed",
        )
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
    public_id = secrets.token_hex(5)
    try:
        cur = conn.execute(
            "INSERT INTO appointments(public_id, patient_id, slot_date, slot_time, cabin,"
            " kind, booked_by, note, moved_from) VALUES (?,?,?,?,?,?,?,?,?)",
            (public_id, patient_id, slot_date, slot_time, free, kind, booked_by, note, moved_from),
        )
    except sqlite3.IntegrityError as exc:
        raise Conflict(
            "این ساعت همین الان پر شد. لطفاً ساعت دیگری انتخاب کنید.",
            code="slot_full",
        ) from exc
    return {
        "id": int(cur.lastrowid or 0),
        "public_id": public_id,
        "cabin": free,
        "slot_date": slot_date,
        "slot_time": slot_time,
        "kind": kind,
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
    with repo.tx() as conn:
        return allocate_on_conn(
            conn,
            patient_id,
            slot_date,
            slot_time,
            kind=kind,
            booked_by=booked_by,
            note=note,
            moved_from=moved_from,
        )


def reschedule(
    old: dict,
    slot_date: str,
    slot_time: str,
    *,
    booked_by: str = "staff",
) -> dict[str, object]:
    """Cancel `old` and book the new slot inside one IMMEDIATE transaction.

    Order is cancel-then-insert so a move can reuse a cabin that the old row
    held (same slot edge cases / capacity accounting). On any failure the
    whole transaction rolls back and `old` stays active.
    """
    validate_slot(slot_date, slot_time)
    with repo.tx() as conn:
        claimed = repo.update(
            SYSTEM,
            "appointments",
            {"status": "cancelled", "updated_at": _utc_now()},
            where="id = ? AND status = ?",
            params=[old["id"], old["status"]],
        )
        if claimed != 1:
            raise Conflict("این نوبت لغو شده است.")
        try:
            return allocate_on_conn(
                conn,
                int(old["patient_id"]),
                slot_date,
                slot_time,
                kind=old["kind"],
                booked_by=booked_by,
                note=old.get("note") or "",
                moved_from=f"{old['slot_date']} {old['slot_time']}",
            )
        except sqlite3.IntegrityError as exc:
            # allocate_on_conn normally maps this; keep a belt-and-braces path for
            # callers/tests that replace allocate_on_conn or hit a race on UPDATE.
            raise Conflict(
                "این ساعت همین الان پر شد. لطفاً ساعت دیگری انتخاب کنید.",
                code="slot_full",
            ) from exc


def describe(appt: dict) -> str:
    return f"{to_jalali_long(appt['slot_date'])} ساعت {appt['slot_time']}"
