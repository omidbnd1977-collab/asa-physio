"""داده‌ی نمونه تا سایت وقتی بالا می‌آید خالی نباشد.

فقط وقتی اجرا می‌شود که جدول patients خالی باشد؛ هیچ‌وقت داده‌ی واقعی را
دست نمی‌زند. کدهای ملی با رقم کنترلِ درست ساخته می‌شوند تا ورود با
«کد ملی + تاریخ تولد» واقعاً کار کند.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import body_lists, sms as smsm
from .config import settings
from .jalali import to_jalali
from .security import normalize_mobile, validate_national_code

FIRST = ["مریم", "سعید", "زهرا", "امیر", "نگار", "رضا", "سمیرا", "کاوه", "الهام", "بهرام",
         "شیوا", "مهدی", "پریسا", "فرهاد", "آیدا", "حسام"]
LAST = ["رضایی", "موسوی", "کاظمی", "شریفی", "نوری", "احمدی", "طاهری", "یزدانی",
        "کریمی", "صادقی", "میرزایی", "فرهادی", "قاسمی", "جعفری", "رحیمی", "دهقان"]

SURNAMES_ORTHO = ["دکتر طهرانی", "دکتر شریفی‌نژاد", "دکتر آذرمهر", None, None]


def make_national_code(seed: int) -> str:
    """کد ملی ۱۰ رقمی با رقم کنترلِ درست."""
    rng = random.Random(seed)
    while True:
        base = "".join(str(rng.randint(0, 9)) for _ in range(9))
        if len(set(base)) < 2:
            continue
        total = sum(int(base[i]) * (10 - i) for i in range(9))
        rem = total % 11
        check = rem if rem < 2 else 11 - rem
        code = base + str(check)
        ok, _msg, norm = validate_national_code(code)
        if ok:
            return norm


def jiso(d: date) -> str:
    jy, jm, jd = to_jalali(d)
    return f"{jy:04d}-{jm:02d}-{jd:02d}"


def seed_if_empty(conn) -> bool:
    from . import db as dbm

    if conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]:
        return False
    body_lists.seed_lists(conn)

    now = settings.now()
    today = now.date()
    times = settings.slot_times()
    conn.execute("BEGIN IMMEDIATE")
    try:
        created: list[int] = []
        people = []
        for i in range(14):
            name = f"{FIRST[i]} {LAST[i]}"
            mobile = normalize_mobile(f"0912{1000000 + i * 731}")[2]
            code = make_national_code(1000 + i)
            birth_j = jiso(today - timedelta(days=365 * (24 + (i * 3) % 34)))
            conn.execute(
                "INSERT INTO patients(full_name, national_code, birth_jdate, mobile,"
                " orthopedist, created_at) VALUES(?,?,?,?,?,?)",
                (name, code, birth_j, mobile, SURNAMES_ORTHO[i % 5], dbm.now_iso()))
            pid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            created.append(pid)
            people.append({"id": pid, "name": name, "mobile": mobile, "code": code,
                           "birth": birth_j})

        hero = people[0]                      # مریم رضایی — بیمارِ بازگشتی
        # دو جلسه‌ی درمانی در گذشته (برای خط زمانی + پیامک «پس از N روز وقفه»)
        for back, parts, treats, pb, pa in [
            (64, ["زانو", "آرتروز زانو"], ["لیزر پرتوان", "تمرین درمانی", "کشش"], 7, 4),
            (58, ["زانو"], ["تکار تراپی", "بیوفیدبک"], 4, 3),
        ]:
            d = jiso(today - timedelta(days=back))
            conn.execute(
                "INSERT INTO patient_sessions(patient_id, appointment_id, session_date,"
                " pain_before, pain_after, findings, next_plan, created_by, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (hero["id"], None, d, pb, pa,
                 "دامنه‌ی خم زانو ۱۰ درجه محدود؛ تورم خفیف.",
                 "ادامه‌ی تمرینات مقاومتی سه جلسه در هفته + یخ بعد از تمرین.",
                 settings.ADMIN_USER, dbm.now_iso()))
            sid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
            for label in parts:
                row = conn.execute("SELECT id FROM body_parts WHERE label = ?", (label,)).fetchone()
                if row:
                    conn.execute("INSERT OR IGNORE INTO session_body_parts(session_id, part_id)"
                                 " VALUES(?,?)", (sid, row["id"]))
            for label in treats:
                row = conn.execute("SELECT id FROM treatments WHERE label = ?", (label,)).fetchone()
                if row:
                    conn.execute("INSERT OR IGNORE INTO session_treatments(session_id, treatment_id)"
                                 " VALUES(?,?)", (sid, row["id"]))
                    conn.execute("UPDATE treatments SET used_count = used_count + 1 WHERE id = ?",
                                 (row["id"],))
        conn.execute("UPDATE patients SET note = ? WHERE id = ?",
                     ("حساسیت به اولتراسوند؛ شدت درد را پایین نگه داریم.", hero["id"]))

        # ۱) نوبتِ امروز ۲۱:۰۰ با «حضور قطعی ✓» (پاسخ ۱ به پیامک)
        booked_rows = []
        conn.execute(
            "INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind,"
            " status, tracking_code, confirm_sent_at, confirm_reply_at, created_at,"
            " updated_at, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (hero["id"], jiso(today), "21:00", 3, "initial", "coming",
             "7KQMX4TB", dbm.now_iso(), dbm.now_iso(), dbm.now_iso(), dbm.now_iso(),
             "زانون، دو جلسه‌ی قبلی انجام شده."))
        aid = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        booked_rows.append((aid, hero, "confirm"))
        conn.execute("INSERT INTO sms_messages(patient_id, appointment_id, event, to_phone,"
                     " body, status, cost_usd, created_at, sent_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (hero["id"], aid, "confirm", hero["mobile"],
                      smsm.render("confirm", name=hero["name"],
                                  jdate="امروز", time="۲۱:۰۰"),
                      "sent", settings.SMS_UNIT_COST_USD, dbm.now_iso(), dbm.now_iso()))
        conn.execute("INSERT INTO sms_inbound(phone, body, normalized, matched_id, created_at)"
                     " VALUES(?,?,?,?,?)", (hero["mobile"], "۱", "yes", aid, dbm.now_iso()))

        # ۲) نوبتِ دیروز: انجام شد
        conn.execute(
            "INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind, status,"
            " tracking_code, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (hero["id"], jiso(today - timedelta(days=1)), "18:30", 2, "followup", "done",
             "PD3WRE9A", dbm.now_iso(), dbm.now_iso()))
        conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'sessions_done'"
                     if conn.execute("SELECT value FROM counters WHERE name='sessions_done'"
                                    ).fetchone() else
                     "INSERT INTO counters(name, value) VALUES('sessions_done', 1)")

        # ۳) پر کردن جدول: فردا ۱۶:۰۰ → ۷ نفر (۳ جای خالی ⇒ زرد)، ۱۶:۳۰ → ۱۰ نفر (پر)
        pool = [p for p in people[1:]]
        rng = random.Random(42)
        slot_counts = {times[0]: 7, times[1]: settings.CABINS}
        for idx in range(2, 6):
            slot_counts.setdefault(times[idx], rng.randint(0, 5))
        for day_offset in (1, 2, 3):
            for t_idx, hhmm in enumerate(times):
                want = slot_counts.get(hhmm, rng.randint(0, 6)) if day_offset == 1 else \
                    rng.randint(0, 6)
                for cabin, pat in enumerate(rng.sample(pool, min(want, len(pool))), 1):
                    status = rng.choices(["booked", "booked", "coming", "done"],
                                         weights=[6, 5, 2, 1])[0]
                    if day_offset > 1:
                        status = "booked" if cabin % 4 else "coming"
                    conn.execute(
                        "INSERT OR IGNORE INTO appointments(patient_id, slot_date, slot_time,"
                        " cabin, kind, status, tracking_code, created_at, updated_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (pat["id"], jiso(today + timedelta(days=day_offset)), hhmm, cabin,
                         "initial", status, _code(rng), dbm.now_iso(), dbm.now_iso()))

        # ۴) یک پیامک با خطای ارسال (تب پیامک‌ها خالی نباشد) + یک لغو
        loser = people[5]
        conn.execute(
            "INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind, status,"
            " tracking_code, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (loser["id"], jiso(today + timedelta(days=4)), "19:00", 5, "initial", "cancelled",
             _code(rng), dbm.now_iso(), dbm.now_iso()))
        conn.execute(
            "INSERT INTO sms_messages(patient_id, event, to_phone, body, status, error,"
            " cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (loser["id"], "cancelled", loser["mobile"],
             "پیامک لغو نوبت", "failed", "smsir: credit exhausted", 0.0, dbm.now_iso()))
        conn.execute(
            "INSERT INTO sms_messages(patient_id, event, to_phone, body, status, error,"
            " cost_usd, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (people[7]["id"], "booked", people[7]["mobile"],
             "پیامک ثبت نوبت", "skipped_budget",
             "سقف ماهانه‌ی ۳$ پر شده بود؛ ارسال نشد.", 0.0, dbm.now_iso()))

        # ۵) هزینه‌های ماه جاری
        for k in range(120):
            conn.execute("INSERT INTO cost_events(kind, amount_usd, ref, created_at)"
                         " VALUES('sms', ?, ?, ?)",
                         (settings.SMS_UNIT_COST_USD, f"sms:seed:{k}", dbm.now_iso()))
        conn.execute("INSERT INTO counters(name, value) VALUES('registrations', ?)"
                     " ON CONFLICT(name) DO UPDATE SET value = value + 14", (0,))

        # ۶) درخواست‌های فرم سریع سایت
        for name, msg, topic in [
            ("آقای کریمی", "بیمه‌ی تکمیلی با شما قرارداد دارد؟", "بیمه"),
            ("سارا", "برای دیسک کمر چه روزهایی وقت هست؟", "سوال"),
            ("خانم دادگر", "پیامک یادآوری نیامد، لطفاً چک کنید.", "پشتیبانی"),
        ]:
            conn.execute(
                "INSERT INTO site_requests(name, phone, topic, message, source, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (name, normalize_mobile("0935" + str(random.randint(1000000, 9999999)))[2],
                 topic, msg, "site quick form", dbm.now_iso()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return True


def _code(rng) -> str:
    from .appointments import new_tracking_code

    return new_tracking_code()
