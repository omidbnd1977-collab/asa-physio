"""هسته‌ی نوبت‌دهی: جدول ۱۲×۱۰، تخصیص کابین، رزرو/تغییر/لغو/جلسه‌ی بعدی.

کنارهم‌زدنِ دو محدودیت داخل `BEGIN IMMEDIATE`:
  * `UNIQUE (slot_date, slot_time, cabin)` → دو نفر در یک کابین نمی‌افتند
  * * `UNIQUE (patient_id, slot_date, slot_time)` → یک نفر دو نوبت هم‌ساعت ندارد
      (نه از مسیر خودش، نه از «تغییر زمان» و «ثبت جلسه‌ی بعدی» پزشک)
هر دو به ۴۰۹ تبدیل می‌شوند: `slot_full` و `already_booked`.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import date, datetime, time as dtime, timedelta

from . import db as dbm
from . import sms as smsm
from .config import settings
from .jalali import (date_range, from_jalali, jalali_of, j_days_between, to_persian_digits,
                     to_jalali)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # بدون 0/O و 1/I
MIN_LEAD_MIN = 60       # رزروِ نوبتی که کمتر از این مقدار به شروعش مانده بسته است
ACTIVE_STATUSES = ("booked", "coming")


class BookingError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ------------------------------------------------------------------ ابزار تاریخ
def jdate_str(d: date) -> str:
    jy, jm, jd = to_jalali(d)
    return f"{jy:04d}-{jm:02d}-{jd:02d}"


def jdate_today() -> str:
    return jdate_str(settings.now().date())


def parse_jdate(value: str) -> tuple[int, int, int]:
    parts = (value or "").split("-")
    if len(parts) != 3:
        raise BookingError(400, "bad_date", "تاریخ باید YYYY-MM-DD شمسی باشد.")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        raise BookingError(400, "bad_date", "تاریخ نامعتبر است.")


def gregorian_of(jdate: str) -> date:
    jy, jm, jd = parse_jdate(jdate)
    return from_jalali(jy, jm, jd)


def slot_datetime(jdate: str, hhmm: str) -> datetime:
    day = gregorian_of(jdate)
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, dtime(hh, mm), tzinfo=settings.tz())


def is_past_slot(jdate: str, hhmm: str) -> bool:
    """گذشته با ساعتِ منطقه‌ی مطب (Asia/Tehran) سنجیده می‌شود، نه UTC."""
    return slot_datetime(jdate, hhmm) <= settings.now() + timedelta(minutes=MIN_LEAD_MIN)


def is_valid_slot_time(hhmm: str) -> bool:
    return hhmm in settings.slot_times()


def jdate_shift(jdate: str, days: int) -> str:
    return jdate_str(gregorian_of(jdate) + timedelta(days=days))


def strip_dates() -> list[date]:
    """نوار ۳۰ روزه: از امروز تا ۲۹ روز بعد."""
    start = settings.now().date()
    return [start + timedelta(days=i) for i in range(settings.STRIP_DAYS)]


def gap_phrase(days: int) -> str:
    if days < 60:
        return f"{to_persian_digits(days)} روز"
    if days < 365:
        months = round(days / 30.4)
        return f"{to_persian_digits(months)} ماه"
    years = round(days / 365.25)
    return f"{to_persian_digits(years)} سال"


# ------------------------------------------------------------------ ظرفیت
def slot_grid(conn: sqlite3.Connection, jdate: str) -> dict:
    """برای یک روز: هر خانه‌ی ساعت + تعداد جای خالی + دلیل غیرفعال بودن."""
    times = settings.slot_times()
    rows = conn.execute(
        f"SELECT slot_time, cabin FROM appointments WHERE slot_date = ? "
        f"AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
        (jdate, *ACTIVE_STATUSES),
    ).fetchall()
    taken: dict[str, set[int]] = {}
    for row in rows:
        taken.setdefault(row["slot_time"], set()).add(int(row["cabin"]))
    per_day = settings.CABINS * settings.CABIN_CAPACITY
    out = []
    for hhmm in times:
        used = len(taken.get(hhmm, ()))
        free = max(per_day - used, 0)
        past = is_past_slot(jdate, hhmm)
        label = "past" if past else ("full" if free == 0 else None)
        out.append({
            "time": hhmm,
            "time_fa": to_persian_digits(hhmm),
            "free": free,
            "few": (not past) and 0 < free <= settings.FEW_LEFT_THRESHOLD,
            "disabled": bool(past or free == 0),
            "reason": label or ("few" if 0 < free <= settings.FEW_LEFT_THRESHOLD else None),
            "capacity": per_day,
        })
    return {
        "jdate": jdate,
        "day": jalali_of(gregorian_of(jdate)),
        "capacity_per_day": per_day * len(times),
        "slots": out,
        "open": any(not s["disabled"] for s in out),
    }


def free_cabin(conn: sqlite3.Connection, jdate: str, hhmm: str,
               exclude_id: int | None = None) -> int:
    """کم‌مصرف‌ترین کابینِ آن خانه، یا -1 اگر همه پر بودند.

    `CABIN_CAPACITY = 1` (پیش‌فرض، طبق «UNIQUE (slot_date, slot_time, cabin)») یعنی
    هر کابین در هر ساعت یک بیمار. اگر روزی خواستید چند بیمار هم‌زمان در یک
    کابین تمرین کنند، همین عدد را بالا ببرید: جدول و ۴۰۹ خودکار درست می‌شوند.
    """
    sql = ("SELECT cabin, COUNT(*) AS n FROM appointments WHERE slot_date=? AND slot_time=?"
           f" AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
    params: list = [jdate, hhmm, *ACTIVE_STATUSES]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " GROUP BY cabin"
    used = {int(r["cabin"]): int(r["n"]) for r in conn.execute(sql, params).fetchall()}
    best, best_n = -1, settings.CABIN_CAPACITY
    for cabin in range(1, settings.CABINS + 1):
        n = used.get(cabin, 0)
        if n < best_n:
            best, best_n = cabin, n
    return best if best_n < settings.CABIN_CAPACITY else -1


# ------------------------------------------------------------------ ثبت نوبت
def new_tracking_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def last_touch_jdate(conn: sqlite3.Connection, patient_id: int, before: str) -> str | None:
    """تازه‌ترین «مراجعه» بیمار: آخرین نوبت **یا** آخرین جلسه‌ی ثبت‌شده — هر کدام تازه‌تر."""
    row = conn.execute(
        "SELECT MAX(d) AS d FROM ("
        "  SELECT MAX(slot_date) AS d FROM appointments WHERE patient_id = ?"
        "    AND status IN ('done','no_show','booked','coming') AND slot_date < ?"
        "  UNION ALL"
        "  SELECT MAX(session_date) AS d FROM patient_sessions WHERE patient_id = ?"
        "    AND session_date < ?"
        ")",
        (patient_id, before, patient_id, before),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def book(conn: sqlite3.Connection, *, patient: sqlite3.Row, jdate: str, hhmm: str,
         kind: str = "initial", idem: str | None = None, note: str = "",
         orthopedist: str | None = None, mri_url: str | None = None,
         enforce_daily_limit: bool = True) -> dict:
    """رزرو داخل یک تراکنش. خطا → BookingError با status/contract روشن."""
    if not is_valid_slot_time(hhmm):
        raise BookingError(400, "bad_time", "ساعت انتخابی در بازه‌ی کاری مطب نیست.")
    if gregorian_of(jdate) < settings.now().date():
        raise BookingError(400, "past_date", "این روز گذشته است.")
    if is_past_slot(jdate, hhmm):
        raise BookingError(409, "slot_past", "این ساعت گذشته است؛ یک خانه‌ی بعدی بردارید.")

    with dbm.tx(immediate=True):
        if enforce_daily_limit and kind == "initial":
            today = jdate_today()
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM appointments WHERE patient_id = ?"
                " AND substr(created_at, 1, 10) = ?",
                (patient["id"], settings.now().strftime("%Y-%m-%d")),
            ).fetchone()["c"]
            if int(n) >= settings.LIMIT_RESERVE_DAY:
                raise BookingError(
                    429, "daily_limit",
                    f"روزانه حداکثر {to_persian_digits(settings.LIMIT_RESERVE_DAY)} رزرو ممکن است.",
                )

        if idem:
            hit = conn.execute(
                "SELECT id FROM appointments WHERE patient_id = ? AND idempotency_key = ?",
                (patient["id"], idem),
            ).fetchone()
            if hit:
                row = conn.execute("SELECT * FROM appointments WHERE id = ?",
                                   (hit["id"],)).fetchone()
                return {"appointment": dict(row), "replayed": True, "sms": [],
                        "gap_days": None}

        clash = conn.execute(
            f"SELECT id, tracking_code FROM appointments WHERE patient_id = ?"
            f" AND slot_date = ? AND slot_time = ? AND status IN "
            f"({','.join('?' * len(ACTIVE_STATUSES))})",
            (patient["id"], jdate, hhmm, *ACTIVE_STATUSES),
        ).fetchone()
        if clash:
            raise BookingError(
                409, "already_booked",
                "شما همین ساعت یک نوبت باز دارید؛ اول همان را جابه‌جا یا لغو کنید.",
            )

        last_before = last_touch_jdate(conn, patient["id"], jdate)
        gap_days = None
        if last_before:
            gap_days = j_days_between(parse_jdate(last_before), parse_jdate(jdate))

        cabin = free_cabin(conn, jdate, hhmm)
        if cabin < 0:   # -1 یعنی همه‌ی کابین‌های آن خانه پر است
            raise BookingError(409, "slot_full", "این ساعت پر شد؛ جدول تازه شد.")

        now = dbm.now_iso()
        code = new_tracking_code()
        try:
            cur = conn.execute(
                "INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind,"
                " status, tracking_code, idempotency_key, note, orthopedist, mri_url,"
                " created_at, updated_at) VALUES(?,?,?,?,?, 'booked', ?,?,?,?,?,?,?)",
                (patient["id"], jdate, hhmm, cabin, kind, code, idem, note or None,
                 orthopedist if orthopedist is not None else patient["orthopedist"],
                 mri_url if mri_url is not None else patient["mri_url"], now, now),
            )
        except sqlite3.IntegrityError as exc:
            msg = str(exc).lower()
            if "slot_date, slot_time, cabin" in msg:
                raise BookingError(409, "slot_full", "دقیقاً هم‌زمان پر شد؛ دوباره بزنید.")
            if "patient_id, slot_date, slot_time" in msg:
                raise BookingError(409, "already_booked", "همین ساعت نوبت دارید.")
            raise BookingError(409, "conflict", str(exc))

        appt_id = cur.lastrowid
        sent = []
        if gap_days is not None and gap_days >= settings.RETURN_GAP_DAYS:
            sent.append(smsm.send(
                conn, event="welcome_back", to=patient["mobile"], patient_id=patient["id"],
                appointment_id=appt_id,
                body=smsm.render("welcome_back", name=patient["full_name"],
                                 gap=gap_phrase(gap_days),
                                 jdate=jalali_of(gregorian_of(jdate))["long"],
                                 time=to_persian_digits(hhmm)),
            ).get("event"))
        sent.append(smsm.send(
            conn, event="booked", to=patient["mobile"], patient_id=patient["id"],
            appointment_id=appt_id,
            body=smsm.render("booked", name=patient["full_name"],
                             jdate=jalali_of(gregorian_of(jdate))["long"],
                             time=to_persian_digits(hhmm), cabin=to_persian_digits(cabin),
                             code=code),
        ).get("event"))
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()
        return {"appointment": dict(row), "replayed": False, "sms": sent,
                "gap_days": gap_days}


# ------------------------------------------------------------------ تغییر زمان
def reschedule(conn: sqlite3.Connection, appointment_id: int, jdate: str, hhmm: str) -> dict:
    """اگر ساعت جدید پر باشد، نوبت قبلی **دست‌نخورده** برمی‌گردد (تستِ مربوطه)."""
    if not is_valid_slot_time(hhmm):
        raise BookingError(400, "bad_time", "ساعت در بازه‌ی کاری نیست.")
    with dbm.tx(immediate=True):
        appt = conn.execute("SELECT * FROM appointments WHERE id = ?",
                             (appointment_id,)).fetchone()
        if not appt:
            raise BookingError(404, "not_found", "نوبت پیدا نشد.")
        if appt["status"] not in ACTIVE_STATUSES + ("done",):
            raise BookingError(409, "bad_status", "این نوبت لغو شده است.")
        if appt["slot_date"] == jdate and appt["slot_time"] == hhmm:
            return {"appointment": dict(appt), "unchanged": True}
        if is_past_slot(jdate, hhmm):
            raise BookingError(409, "slot_past", "ساعت جدید گذشته است.")

        clash = conn.execute(
            f"SELECT id FROM appointments WHERE patient_id = ? AND slot_date = ?"
            f" AND slot_time = ? AND id != ? AND status IN "
            f"({','.join('?' * len(ACTIVE_STATUSES))})",
            (appt["patient_id"], jdate, hhmm, appointment_id, *ACTIVE_STATUSES),
        ).fetchone()
        if clash:
            raise BookingError(409, "already_booked",
                               "این بیمار ساعت مقصد نوبت باز دیگری دارد.")

        # نوبتِ در حال جابه‌جایی شمرده نمی‌شود؛ جای جدید از همان free_cabin می‌آید
        cabin = free_cabin(conn, jdate, hhmm, exclude_id=appointment_id)
        if cabin < 0:
            raise BookingError(409, "slot_full", "ساعت مقصد پر است؛ نوبت قبلی دست‌نخورده ماند.")

        try:
            conn.execute(
                "UPDATE appointments SET slot_date=?, slot_time=?, cabin=?,"
                " slot_prev_date=slot_date, slot_prev_time=slot_time, cabin_prev=cabin,"
                " kind='reschedule', confirm_sent_at=NULL, reminder_attempts=0,"
                " updated_at=? WHERE id=?",
                (jdate, hhmm, cabin, dbm.now_iso(), appointment_id),
            )
        except sqlite3.IntegrityError as exc:
            raise BookingError(409, "slot_full",
                              f"همان لحظه پر شد ({exc}). نوبت قبلی دست‌نخورده ماند.")

        patient = conn.execute("SELECT * FROM patients WHERE id = ?",
                              (appt["patient_id"],)).fetchone()
        prev = {"jdate": jalali_of(gregorian_of(appt["slot_date"]))["long"],
                "time": to_persian_digits(appt["slot_time"])}
        smsm.send(conn, event="rescheduled", to=patient["mobile"],
                  patient_id=patient["id"], appointment_id=appointment_id,
                  body=smsm.render("rescheduled", name=patient["full_name"],
                                   prev_jdate=prev["jdate"], prev_time=prev["time"],
                                   jdate=jalali_of(gregorian_of(jdate))["long"],
                                   time=to_persian_digits(hhmm),
                                   cabin=to_persian_digits(cabin),
                                   code=appt["tracking_code"]))
        row = conn.execute("SELECT * FROM appointments WHERE id = ?",
                           (appointment_id,)).fetchone()
        return {"appointment": dict(row), "previous": prev}


def set_status(conn: sqlite3.Connection, appointment_id: int, status: str,
               send_sms: bool = False) -> dict:
    if status not in {"booked", "coming", "done", "cancelled", "no_show"}:
        raise BookingError(400, "bad_status", "وضعیت نامعتبر است.")
    with dbm.tx(immediate=True):
        appt = conn.execute("SELECT * FROM appointments WHERE id = ?",
                            (appointment_id,)).fetchone()
        if not appt:
            raise BookingError(404, "not_found", "نوبت پیدا نشد.")
        conn.execute("UPDATE appointments SET status = ?, updated_at = ? WHERE id = ?",
                     (status, dbm.now_iso(), appointment_id))
        if status == "done":
            # «عدد اصلی لایه ۱۴» — شمارنده‌ی جلسات انجام‌شده
            dbm.bump(conn, "sessions_done")
        patient = conn.execute("SELECT * FROM patients WHERE id = ?",
                               (appt["patient_id"],)).fetchone()
        if send_sms:
            event = "cancelled" if status == "cancelled" else "confirm"
            if event == "cancelled":
                body = smsm.render("cancelled", name=patient["full_name"],
                                   jdate=jalali_of(gregorian_of(appt["slot_date"]))["long"],
                                   time=to_persian_digits(appt["slot_time"]))
            else:
                body = smsm.render("confirm", name=patient["full_name"],
                                   jdate=jalali_of(gregorian_of(appt["slot_date"]))["long"],
                                   time=to_persian_digits(appt["slot_time"]))
            smsm.send(conn, event=event, to=patient["mobile"], patient_id=patient["id"],
                       appointment_id=appointment_id, body=body)
        row = conn.execute("SELECT * FROM appointments WHERE id = ?",
                           (appointment_id,)).fetchone()
        return {"appointment": dict(row)}


def apply_attendance(conn: sqlite3.Connection, appointment_id: int, value: str) -> dict | None:
    """بدون تراکنش: برای فراخوانی از داخل یک تراکنشِ باز (مثل وبهوک پیامک)."""
    if value not in {"coming", "not_coming"}:
        raise BookingError(400, "bad_value", "مقدار نامعتبر.")
    conn.execute(
        "UPDATE appointments SET status = CASE WHEN ? = 'coming' THEN 'coming'"
        " ELSE status END, confirm_reply_at = ?, updated_at = ? WHERE id = ?",
        (value, dbm.now_iso(), dbm.now_iso(), appointment_id),
    )
    row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    return dict(row) if row else None


def mark_attendance(conn: sqlite3.Connection, appointment_id: int, value: str) -> dict:
    with dbm.tx(immediate=True):
        return {"appointment": apply_attendance(conn, appointment_id, value), "value": value}


def cancel_by_patient(conn: sqlite3.Connection, appointment_id: int, patient_id: int) -> dict:
    with dbm.tx(immediate=True):
        appt = conn.execute("SELECT * FROM appointments WHERE id = ? AND patient_id = ?",
                            (appointment_id, patient_id)).fetchone()
        if not appt:
            raise BookingError(404, "not_found", "نوبت شما پیدا نشد.")
        if appt["status"] in ("done", "cancelled"):
            raise BookingError(409, "bad_status", "این نوبت قابل لغو نیست.")
        conn.execute("UPDATE appointments SET status='cancelled', updated_at=? WHERE id=?",
                     (dbm.now_iso(), appointment_id))
        patient = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
        smsm.send(conn, event="cancelled", to=patient["mobile"], patient_id=patient_id,
                  appointment_id=appointment_id,
                  body=smsm.render("cancelled", name=patient["full_name"],
                                   jdate=jalali_of(gregorian_of(appt["slot_date"]))["long"],
                                   time=to_persian_digits(appt["slot_time"])))
        row = conn.execute("SELECT * FROM appointments WHERE id = ?",
                            (appointment_id,)).fetchone()
        return {"appointment": dict(row)}


# ------------------------------------------------------------------ جلسه‌ی درمان
def record_session(conn: sqlite3.Connection, *, patient_id: int, appointment_id: int | None,
                   jdate: str, parts: list[str], treatments: list[str],
                   pain_before: int | None, pain_after: int | None,
                   findings: str, next_plan: str, add_parts: list[str],
                   add_treatments: list[str], notify: bool,
                   by: str = "dr-asa") -> dict:
    """با ثبت جلسه، آن نوبت خودکار «انجام شد» می‌شود و شمارنده بالا می‌رود."""
    with dbm.tx(immediate=True):
        now = dbm.now_iso()
        for label in [x.strip() for x in add_parts if x.strip()]:
            conn.execute(
                "INSERT INTO body_parts(label, grp, source, created_at) VALUES(?,?, 'physician', ?)"
                " ON CONFLICT(label) DO NOTHING", (label, "سایر", now))
        for label in [x.strip() for x in add_treatments if x.strip()]:
            conn.execute(
                "INSERT INTO treatments(label, source, created_at) VALUES(?, 'physician', ?)"
                " ON CONFLICT(label) DO NOTHING", (label, now))

        part_ids = _resolve(conn, "body_parts", parts)
        treat_ids = _resolve(conn, "treatments", treatments)
        cur = conn.execute(
            "INSERT INTO patient_sessions(patient_id, appointment_id, session_date,"
            " pain_before, pain_after, findings, next_plan, created_by, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (patient_id, appointment_id, jdate, pain_before, pain_after,
             findings or None, next_plan or None, by, now),
        )
        sid = cur.lastrowid
        for pid in part_ids:
            conn.execute("INSERT OR IGNORE INTO session_body_parts(session_id, part_id)"
                         " VALUES(?,?)", (sid, pid))
            conn.execute("UPDATE body_parts SET used_count = used_count + 1 WHERE id = ?", (pid,))
        for tid in treat_ids:
            conn.execute("INSERT OR IGNORE INTO session_treatments(session_id, treatment_id)"
                         " VALUES(?,?)", (sid, tid))
            conn.execute("UPDATE treatments SET used_count = used_count + 1 WHERE id = ?", (tid,))

        touched = None
        if appointment_id:
            appt = conn.execute("SELECT * FROM appointments WHERE id = ?",
                                 (appointment_id,)).fetchone()
            if appt and appt["status"] in ACTIVE_STATUSES:
                conn.execute("UPDATE appointments SET status='done', updated_at=? WHERE id=?",
                             (now, appointment_id))
                dbm.bump(conn, "sessions_done")
                touched = appt["tracking_code"]

        sms = None
        if notify:
            patient = conn.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
            labels = ", ".join(t["label"] for t in conn.execute(
                "SELECT b.label FROM session_treatments s JOIN treatments b ON b.id = s.treatment_id"
                " WHERE s.session_id = ?", (sid,)).fetchall()) or "درمان ثبت‌شده"
            if pain_before is not None and pain_after is not None:
                body = smsm.render("session_summary", name=patient["full_name"],
                                   jdate=jalali_of(gregorian_of(jdate))["long"],
                                   treatments=labels,
                                   pain_before=to_persian_digits(pain_before),
                                   pain_after=to_persian_digits(pain_after))
            else:
                body = smsm.render("session_summary_nopain", name=patient["full_name"],
                                   jdate=jalali_of(gregorian_of(jdate))["long"],
                                   treatments=labels)
            sms = smsm.send(conn, event="session_summary", to=patient["mobile"],
                            patient_id=patient_id, appointment_id=appointment_id,
                            body=body)["status"]
        return {"session_id": sid, "appointment_done": touched, "sms": sms}


def _resolve(conn: sqlite3.Connection, table: str, labels: list[str]) -> list[int]:
    ids = []
    for label in [x.strip() for x in labels if x and x.strip()]:
        row = conn.execute(f"SELECT id FROM {table} WHERE label = ? COLLATE NOCASE",
                           (label,)).fetchone()
        if row:
            ids.append(int(row["id"]))
    return ids


def timeline(conn: sqlite3.Connection, patient_id: int) -> list[dict]:
    """خط زمانی از جدید به قدیم + نشان «پس از N روز وقفه»."""
    rows = conn.execute(
        "SELECT s.*, (SELECT GROUP_CONCAT(b.label, '، ') FROM session_body_parts sb"
        "  JOIN body_parts b ON b.id = sb.part_id WHERE sb.session_id = s.id) AS parts,"
        " (SELECT GROUP_CONCAT(t.label, '، ') FROM session_treatments st"
        "  JOIN treatments t ON t.id = st.treatment_id WHERE st.session_id = s.id) AS treatments"
        " FROM patient_sessions s WHERE s.patient_id = ? ORDER BY s.session_date, s.id",
        (patient_id,),
    ).fetchall()
    out = []
    prev = None
    for r in rows:                      # از قدیم به تازه؛ نشان وقفه به جلسه‌ی تازه می‌چسبد
        gap = None
        if prev:
            g = j_days_between(parse_jdate(prev), parse_jdate(r["session_date"]))
            if g >= settings.RETURN_GAP_DAYS:
                gap = g
        out.append({
            "date": jalali_of(gregorian_of(r["session_date"])),
            "parts": r["parts"] or "", "treatments": r["treatments"] or "",
            "pain_before": r["pain_before"], "pain_after": r["pain_after"],
            "findings": r["findings"] or "", "next_plan": r["next_plan"] or "",
            "gap_days": gap,
        })
        prev = r["session_date"]
    out.reverse()                       # نمایش: جدید به قدیم
    return out


def patient_appointments(conn: sqlite3.Connection, patient_id: int, upcoming_only: bool = False):
    q = ("SELECT a.*, p.full_name FROM appointments a JOIN patients p ON p.id = a.patient_id"
         " WHERE a.patient_id = ?")
    if upcoming_only:
        q += f" AND a.status IN ({','.join('?' * len(ACTIVE_STATUSES))}) AND a.slot_date >= ?"
        rows = conn.execute(q + " ORDER BY a.slot_date, a.slot_time",
                            (patient_id, *ACTIVE_STATUSES, jdate_today())).fetchall()
    else:
        rows = conn.execute(q + " ORDER BY a.slot_date DESC, a.slot_time DESC",
                            (patient_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["day"] = jalali_of(gregorian_of(d["slot_date"]))
        d["time_fa"] = to_persian_digits(d["slot_time"])
        d["cabin_fa"] = to_persian_digits(d["cabin"])
        d["cancelable"] = d["status"] in ACTIVE_STATUSES and not is_past_slot(
            d["slot_date"], d["slot_time"])
        out.append(d)
    return out
