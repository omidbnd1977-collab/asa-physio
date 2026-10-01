"""آزمون‌های سامانه — `python3 scripts/check.py`

هر آزمون یک پایگاه‌داده‌ی تازه دارد. آزمونِ `test_a_failed_reschedule_leaves_the_original_intact`
همان چیزی است که BOOKING.md به‌عنوان تست نام برده.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import appointments as appts          # noqa: E402
from api import db as dbm                      # noqa: E402
from api import reminders                      # noqa: E402
from api import sms as smsm                    # noqa: E402
from api.app import app                        # noqa: E402
from api.config import settings                # noqa: E402
from api.jalali import from_jalali, j_days_between, to_jalali   # noqa: E402
from api.jalali import PERSIAN_DIGITS as FA_DIGITS    # noqa: E402
from api.security import (limiter, normalize_mobile, parse_jalali_date,   # noqa: E402
                          upload_path, validate_national_code)

RESULTS: list[tuple[str, bool, str]] = []


def fresh():
    tmp = Path(tempfile.mkdtemp(prefix="asa-test-"))
    settings.DATA_DIR = tmp
    settings.DB_PATH = tmp / "t.db"
    settings.UPLOAD_DIR = tmp / "uploads"
    settings.SMS_LOG_PATH = tmp / "sms.log"
    settings.APP_ENV = "testing"
    settings.SMS_PROVIDER = "file"
    settings.SMS_BUDGET_MONTHLY_USD = 3.0
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    dbm.reset_for_tests()
    dbm.init()
    limiter._hits.clear()
    return tmp


def client():
    return app.test_client()


def csrf_cookie(c):
    ck = c.get_cookie(settings.CSRF_COOKIE_NAME)
    return ck.value if ck else ""


def post_json(c, path, body):
    """همان کاری که site.js می‌کند: هدر X-CSRF-Token از کوکی."""
    tok = csrf_cookie(c) or csrf_of(c)
    return c.post(path, json=body, headers={"X-CSRF-Token": tok})


def csrf_of(c, page="/booking/register"):
    c.get(page)
    ck = c.get_cookie(settings.CSRF_COOKIE_NAME)
    return ck.value if ck else ""


def register(c, name="مریم رضایی", code=None, y="۱۳۶۵", m="۲", d="۱۵",
             mobile="۰۹۱۲۱۱۱۲۲۳۳", csrf=None, **kw):
    if csrf is None:
        csrf = csrf_of(c)
    code = code or GOOD_NC
    form = {"full_name": name, "national_code": code, "birth_year": y, "birth_month": m,
            "birth_day": d, "mobile": mobile, "csrf_token": csrf}
    form.update(kw)
    return c.post("/booking/register", data=form)


from api.seed import make_national_code as _valid_code   # noqa: E402

GOOD_NC = _valid_code(7)
GOOD_NC_ALT = _valid_code(8)
BAD_NC = (GOOD_NC[:-1] + str((int(GOOD_NC[-1]) + 1) % 10))


def jiso(d: date) -> str:
    jy, jm, jd = to_jalali(d)
    return f"{jy:04d}-{jm:02d}-{jd:02d}"


def next_open_day(c):
    """نخستین روزی که دست‌کم یک خانه باز دارد (فردا)."""
    return jiso(settings.now().date() + timedelta(days=1))


def check(name):
    def deco(fn):
        def runner():
            tmp = fresh()
            try:
                fn()
                RESULTS.append((fn.__name__, True, ""))
                print(f"  ✓ {name}")
            except Exception as exc:
                RESULTS.append((fn.__name__, False, f"{type(exc).__name__}: {exc}"))
                print(f"  ✗ {name}\n{traceback.format_exc()}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        runner.__name__ = fn.__name__
        globals()[fn.__name__] = runner
        return runner
    return deco


# ─────────────────────────────── تقویم و ورودی‌ها
@check("تقویم شمسی: امروز، رفت‌وبرگشت، و اختلاف روز")
def _t0():
    assert to_jalali(date(2026, 10, 1)) == (1405, 7, 9)
    assert from_jalali(1405, 7, 9) == date(2026, 10, 1)
    assert j_days_between((1405, 5, 1), (1405, 7, 9)) == 70   # تیر ۳۱ + مرداد ۳۱ + ۸


@check("کد ملی: رقم کنترل، ارقام فارسی، و شکل ۸ رقمی")
def _t1():
    assert validate_national_code(GOOD_NC)[0] is True
    assert validate_national_code(GOOD_NC.lstrip("0").zfill(8))[0] is True   # شکل ۸ رقمی
    assert validate_national_code("".join(FA_DIGITS[int(x)] for x in GOOD_NC))[0] is True
    assert validate_national_code(BAD_NC)[0] is False
    assert validate_national_code("1111111111")[0] is False
    assert validate_national_code("1234567")[0] is False


@check("نرمال‌سازی موبایل: +98 / 0098 / 09 / ارقام فارسی")
def _t2():
    want = "00989121112233"
    for raw in ["+98 912 111 22 33", "00989121112233", "۰۹۱۲۱۱۱۲۲۳۳",
                "0912-111-2233", "+۹۸۹۱۲۱۱۱۲۲۳۳"]:
        ok, msg, got = normalize_mobile(raw)
        assert ok and got == want, (raw, ok, msg, got)
    assert normalize_mobile("02188776655")[0] is False
    assert normalize_mobile("1234")[0] is False


@check("ماسک تاریخ تولد: ارقام فارسی و ردِ روز ناموجود")
def _t3():
    assert parse_jalali_date("۱۳۶۵", "۰۲", "۱۵")[2] == (1365, 2, 15)
    assert parse_jalali_date("1365", "12", "30")[0] is False     # اسفند ۲۹/۳۰ روزه
    assert parse_jalali_date("1365", "1", "1")[0] is True
    assert parse_jalali_date("2026", "1", "1")[0] is False       # سال نامعتبر
    assert parse_jalali_date("1405", "12", "1")[0] is False      # آینده


# ─────────────────────────────── مسیر بیمار
@check("ثبت‌نام → پیامک «ثبت شد» → نشست → جدول نوبت‌ها")
def _t4():
    c = client()
    r = register(c)
    assert r.status_code in (200, 302), r.status_code
    n = dbm.db().execute("SELECT COUNT(*) c FROM sms_messages WHERE event='registered'").fetchone()["c"]
    assert n == 1
    body = dbm.db().execute("SELECT body FROM sms_messages WHERE event='registered'").fetchone()["body"]
    assert "آسا فیزیو ثبت شد" in body
    r2 = c.get("/booking/reserve")
    assert r2.status_code == 200
    assert "جدول نوبت‌ها" in r2.get_data(as_text=True)


@check("کد ملی اشتباه در ثبت‌نام ⇒ ۴۰۰ و هیچ رکوردی ساخته نمی‌شود")
def _t5():
    c = client()
    r = register(c, code=BAD_NC)
    assert r.status_code == 400
    assert dbm.db().execute("SELECT COUNT(*) c FROM patients").fetchone()["c"] == 0


@check("ورود: کد ملی + تاریخ تولد لازم است؛ یکی اشتباه ⇒ ۴۰۱")
def _t6():
    c = client()
    register(c)
    c.get("/booking/logout")
    ok = c.post("/booking/login", data={"national_code": GOOD_NC, "birth_year": "1365",
                                        "birth_month": "2", "birth_day": "15", "csrf_token": "x"})
    assert ok.status_code in (200, 302), ok.status_code
    c.get("/booking/logout")
    bad = c.post("/booking/login", data={"national_code": GOOD_NC, "birth_year": "1365",
                                        "birth_month": "2", "birth_day": "16"})
    assert bad.status_code == 401, bad.status_code
    c.get("/booking/logout")
    bad2 = c.post("/booking/login", data={"national_code": BAD_NC, "birth_year": "1365",
                                         "birth_month": "2", "birth_day": "15"})
    assert bad2.status_code == 401


@check("پس از ۵ خطا، ورودِ آن کد ملی قفل می‌شود (۴۲۹)")
def _t7():
    c = client()
    register(c)
    c.get("/booking/logout")
    codes = []
    for i in range(8):
        r = c.post("/booking/login", data={"national_code": GOOD_NC, "birth_year": "1365",
                                           "birth_month": "2", "birth_day": "1" + str(i)})
        codes.append(r.status_code)
    assert 429 in codes, codes
    # کد ملیِ درست هم تا باز شدن قفل نمی‌تواند وارد شود
    r = c.post("/booking/login", data={"national_code": GOOD_NC, "birth_year": "1365",
                                       "birth_month": "2", "birth_day": "15"})
    assert r.status_code == 429, r.status_code


@check("سقف ثبت‌نام: ۳ بار در روز به ازای کد ملی")
def _t8():
    c = client()
    statuses = [register(c).status_code for _ in range(4)]
    assert statuses[:3] == [302, 302, 302], statuses       # رکورد به‌روز می‌شود → جدول نوبت‌ها
    assert statuses[3] == 429, statuses


# ─────────────────────────────── ظرفیت و رزرو
@check("جدول ۱۲ خانه از ۱۶:۰۰ تا ۲۱:۳۰ و ظرفیت ۱۲۰ نوبت در روز")
def _t9():
    fresh()
    times = settings.slot_times()
    assert len(times) == 12, times
    assert times[0] == "16:00" and times[-1] == "21:30"
    conn = dbm.db()
    g = appts.slot_grid(conn, next_open_day(None))
    assert g["capacity_per_day"] == settings.CABINS * settings.CABIN_CAPACITY * 12 == 120
    assert all(s["free"] == 10 for s in g["slots"])


@check("ساعت گذشته و ساعت پر ⇒ disabled (کلیک و کیبورد رد می‌شود)")
def _t10():
    c = client()
    register(c)
    today = jiso(settings.now().date())
    g = appts.slot_grid(dbm.db(), today)
    assert any(s["reason"] == "past" for s in g["slots"])       # الان بعد از ۱۶:۰۰ است
    assert all(s["disabled"] for s in g["slots"] if s["reason"] == "past")
    r = post_json(c, "/api/reserve", {"date": today, "time": "16:00"})
    assert r.status_code in (400, 409), r.status_code


@check("دو نفر هم‌زمان آخرین جای خالی ⇒ یکی ۴۰۹ slot_full و کابین‌ها یکتا")
def _t11():
    tmp = fresh()
    c = client()
    register(c)
    day = next_open_day(None)
    conn = dbm.db()
    # ۹ صندلی را پر می‌کنیم
    for i in range(1, settings.CABINS):
        norm = _valid_code(900 + i)
        conn.execute("INSERT OR IGNORE INTO patients(full_name, national_code, birth_jdate,"
                     " mobile, created_at) VALUES(?,?,?,?,?)",
                     (f"بیمار {i}", norm, "1360-01-01",
                      f"0098912000{i:04d}", dbm.now_iso()))
        conn.execute("INSERT OR IGNORE INTO appointments(patient_id, slot_date, slot_time,"
                     " cabin, status, tracking_code, created_at, updated_at) VALUES(?,?,?,"
                     " ?, 'booked', ?, ?, ?)",
                     (i + 1, day, "16:00", i + 1, f"X{i:07d}", dbm.now_iso(), dbm.now_iso()))
    used = {r["cabin"] for r in conn.execute(
        "SELECT cabin FROM appointments WHERE slot_date=? AND slot_time=?",
        (day, "16:00")).fetchall()}
    free = set(range(1, settings.CABINS + 1)) - used
    assert len(free) == 1, (used, free)

    results = []
    barrier = threading.Barrier(2)

    errors = []

    errors = []

    def racer(tag):
        try:
            cc = client()
            register(cc, name=f"مسابقه‌ای {tag}",
                     code=GOOD_NC if tag == "a" else GOOD_NC_ALT,
                     mobile=f"0912111223{3 if tag == 'a' else 4}")
            barrier.wait(20)
            r = post_json(cc, "/api/reserve", {"date": day, "time": "16:00"})
            results.append((r.status_code, r.get_json()))
        except Exception as exc:              # خطای رشته وگرنه گم می‌شود
            errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    ths = [threading.Thread(target=racer, args=(t,)) for t in ("a", "b")]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    codes = sorted(r[0] for r in results)
    assert codes == [200, 409], (codes, errors, len(results))
    lost = [r for r in results if r[0] == 409][0]
    assert lost[1]["error"] == "slot_full", lost[1]
    assert "grid" in lost[1] and lost[1]["grid"]["slots"][0]["free"] == 0
    rows = conn.execute("SELECT cabin FROM appointments WHERE slot_date=? AND slot_time=?",
                        (day, "16:00")).fetchall()
    assert len({r["cabin"] for r in rows}) == settings.CABINS, "دو نفر در یک کابین افتادند!"


@check("یک بیمار نمی‌تواند دو نوبت هم‌ساعت بگیرد (حتی از پنل پزشک)")
def _t12():
    c = client()
    register(c)
    day = next_open_day(None)
    c.get("/booking/register")
    csrf = csrf_of(c)
    r1 = post_json(c, "/api/reserve", {"date": day, "time": "17:00", "csrf_token": csrf,
                                      "idempotency_key": "k1"})
    assert r1.status_code == 200, r1.get_json()
    r2 = post_json(c, "/api/reserve", {"date": day, "time": "17:00", "csrf_token": csrf,
                                      "idempotency_key": "k2"})
    assert r2.status_code == 409 and r2.get_json()["error"] == "already_booked", r2.get_json()


@check("کلیک دوباره (همان idempotency_key) نوبت دوم نمی‌سازد و سقف روزانه را نمی‌سوزاند")
def _t13():
    c = client()
    register(c)
    day = next_open_day(None)
    csrf = csrf_of(c)
    body = {"date": day, "time": "18:00", "csrf_token": csrf, "idempotency_key": "same-key"}
    a = post_json(c, "/api/reserve", body)
    b = post_json(c, "/api/reserve", body)
    assert a.status_code == 200 and b.status_code == 200
    assert b.get_json()["replayed"] is True
    assert a.get_json()["appointment"]["tracking_code"] == b.get_json()["appointment"]["tracking_code"]
    n = dbm.db().execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"]
    assert n == 1


@check("سقف روزانه‌ی بیمار: ۴ رزرو در روز")
def _t14():
    c = client()
    register(c)
    day = next_open_day(None)
    out = [post_json(c, "/api/reserve", {"date": day, "time": t,
                                         "idempotency_key": f"k{i}"}).status_code
           for i, t in enumerate(["16:00", "16:30", "17:00", "17:30", "18:00"])]
    assert out[:4] == [200] * 4, out
    assert out[4] == 429, out


@check("رسید: کد پیگیری، تاریخ، آدرس — و کد نامعتبر ۴۰۴‌طور است")
def _t15():
    c = client()
    register(c)
    r = post_json(c, "/api/reserve", {"date": next_open_day(None), "time": "19:00",
                                     "idempotency_key": "k"})
    code = r.get_json()["appointment"]["tracking_code"]
    page = c.get("/booking/done?code=" + code).get_data(as_text=True)
    assert "کد پیگیری" in page and code in page and settings.ADDRESS in page
    missing = c.get("/booking/done?code=NOPE1234").get_data(as_text=True)
    assert "پیدا نشد" in missing


# ─────────────────────────────── پنل پزشک
def admin_login(c):
    c.get("/admin/login")
    csrf = csrf_of(c)
    r = c.post("/admin/login", data={"user": settings.ADMIN_USER,
                                     "password": settings.ADMIN_PASSWORD, "csrf_token": csrf})
    assert r.status_code in (200, 302), r.status_code
    return csrf


@check("پنل پزشک (با داده‌ی نمونه‌ی کامل): ۵ تب؛ بدون ورود ⇒ ۴۰۳ روی API")
def _t16():
    from api.seed import seed_if_empty
    c = client()
    seed_if_empty(dbm.db())          # تب‌ها باید با داده‌ی واقعی هم رندر شوند
    admin_login(c)
    r = c.get("/admin")
    assert r.status_code == 200, r.get_data(as_text=True)[:400]
    html = r.get_data(as_text=True)
    for tab in ("نوبت‌ها", "بیماران", "پیامک‌ها", "درخواست‌های سایت", "سامانه"):
        assert tab in html, tab
    anon = client()
    assert anon.get("/api/admin/slot-grid").status_code == 403


@check("تغییر زمان به ساعت پر ⇒ ۴۰۹ و نوبت قبلی دست‌نخورده (تستِ نام‌برده در BOOKING.md)")
def test_a_failed_reschedule_leaves_the_original_intact():
    c = client()
    register(c)
    day = next_open_day(None)
    r = post_json(c, "/api/reserve", {"date": day, "time": "16:00", "idempotency_key": "k"})
    aid = r.get_json()["appointment"]["id"]
    csrf = admin_login(c)
    conn = dbm.db()
    # همه‌ی کابین‌های ۱۶:۳۰ را با بیماران دیگر پر می‌کنیم
    for i in range(1, settings.CABINS + 1):
        conn.execute("INSERT INTO patients(full_name, national_code, birth_jdate, mobile,"
                     " created_at) VALUES(?,?,?,?,?)",
                     (f"اشغال‌کننده {i}", _valid_code(500 + i), "1360-01-01",
                      f"009891222{i:05d}", dbm.now_iso()))
        filler = int(conn.execute("SELECT last_insert_rowid() i").fetchone()["i"])
        conn.execute("INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, status,"
                     " tracking_code, created_at, updated_at) VALUES(?,?,?,?,'booked',?,?,?)",
                     (filler, day, "16:30", i, f"FULL{i:05d}", dbm.now_iso(), dbm.now_iso()))
    before = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone())
    res = c.post(f"/api/admin/appointments/{aid}/reschedule",
                 json={"date": day, "time": "16:30", "csrf_token": csrf})
    assert res.status_code == 409, res.get_json()
    assert res.get_json()["error"] == "slot_full"
    after = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone())
    for key in ("slot_date", "slot_time", "cabin", "status", "tracking_code", "kind"):
        assert before[key] == after[key], (key, before[key], after[key])
    assert res.get_json()["original"]["slot_time"] == "16:00"


@check("تغییر زمان موفق ⇒ پیامک «تغییر نوبت» با زمان قبلی و جدید")
def _t18():
    c = client()
    register(c)
    day = next_open_day(None)
    r = post_json(c, "/api/reserve", {"date": day, "time": "20:00", "idempotency_key": "k"})
    aid = r.get_json()["appointment"]["id"]
    csrf = admin_login(c)
    res = c.post(f"/api/admin/appointments/{aid}/reschedule",
                 json={"date": day, "time": "20:30", "csrf_token": csrf})
    assert res.status_code == 200, res.get_json()
    m = dbm.db().execute("SELECT body, event FROM sms_messages WHERE event='rescheduled'"
                         " ORDER BY id DESC LIMIT 1").fetchone()
    assert m and "۲۰:۰۰" in m["body"] and "۲۰:۳۰" in m["body"], m
    assert dbm.db().execute("SELECT cabin, slot_prev_time FROM appointments WHERE id=?",
                            (aid,)).fetchone()["slot_prev_time"] == "20:00"


@check("ثبت جلسه‌ی بعدی (followup) پیامکِ جلسه بعدی می‌فرستد")
def _t19():
    c = client()
    register(c)
    day = next_open_day(None)
    r = post_json(c, "/api/reserve", {"date": day, "time": "16:00", "idempotency_key": "k"})
    aid = r.get_json()["appointment"]["id"]
    csrf = admin_login(c)
    res = c.post(f"/api/admin/appointments/{aid}/followup",
                 json={"date": jiso(settings.now().date() + timedelta(days=2)),
                       "time": "17:30", "csrf_token": csrf})
    assert res.status_code == 200, res.get_json()
    kind = dbm.db().execute("SELECT kind FROM appointments ORDER BY id DESC LIMIT 1").fetchone()["kind"]
    assert kind == "followup"
    assert dbm.db().execute("SELECT COUNT(*) c FROM sms_messages WHERE event='followup'").fetchone()["c"] == 1


@check("ثبت جلسه‌ی درمان: نوبت «انجام شد»، شمارنده بالا می‌رود، کرکره رشد می‌کند")
def _t20():
    c = client()
    register(c)
    day = next_open_day(None)
    r = post_json(c, "/api/reserve", {"date": day, "time": "19:30", "idempotency_key": "k"})
    j = r.get_json()["appointment"]
    csrf = admin_login(c)
    body = {"patient_id": j["patient_id"], "appointment_id": j["id"], "date": day,
            "parts": "زانو|دیس کمر", "treatments": "لیزر پرتوان|تمرین درمانی",
            "pain_before": "۷", "pain_after": "۳", "findings": "دامنه خم ۱۰° محدود",
            "next_plan": "تمرین مقاومتی", "add_parts": "آسیب ناشناخته‌ی جدید",
            "add_treatments": "درمان آزمایشی", "notify": "1", "csrf_token": csrf}
    res = c.post("/api/admin/sessions", json=body)
    assert res.status_code == 200, res.get_json()
    conn = dbm.db()
    assert conn.execute("SELECT status FROM appointments WHERE id=?", (j["id"],)).fetchone()["status"] == "done"
    assert conn.execute("SELECT value FROM counters WHERE name='sessions_done'").fetchone()["value"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM body_parts WHERE source='physician'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM treatments WHERE source='physician'").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sms_messages WHERE event='session_summary'").fetchone()["c"] == 1
    # تکراری رد می‌شود
    dup = c.post("/api/admin/items", json={"kind": "part", "label": "آسیب ناشناخته‌ی جدید",
                                           "csrf_token": csrf})
    assert dup.status_code == 409


@check("خط زمانی: وقفه‌ی ≥ ۳۰ روز با نشان «پس از N روز وقفه»")
def _t21():
    c = client()
    register(c)
    csrf = admin_login(c)
    conn = dbm.db()
    pid = conn.execute("SELECT id FROM patients LIMIT 1").fetchone()["id"]
    today = settings.now().date()
    for back in (70, 30, 10):
        d = jiso(today - timedelta(days=back))
        c.post("/api/admin/sessions", json={"patient_id": pid, "date": d, "parts": "کمر",
                                           "treatments": "ماساژ", "csrf_token": csrf})
    tl = appts.timeline(conn, pid)
    gaps = [t["gap_days"] for t in tl]          # ترتیب نمایش: تازه → قدیم
    assert gaps == [None, 40, None], gaps       # ۱۰ روز پیش بی‌وقفه؛ ۳۰ روز پیش پس از ۴۰ روز


@check("پیامک تشکر بازگشت: فقط وقتی ≥ ۳۰ روز فاصله باشد")
def _t22():
    c = client()
    register(c)
    conn = dbm.db()
    pid = conn.execute("SELECT id FROM patients").fetchone()["id"]
    today = settings.now().date()
    conn.execute("INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind,"
                 " status, tracking_code, created_at, updated_at) VALUES(?,?,?,'1','initial',"
                 " 'done',?,?,?)", (pid, jiso(today - timedelta(days=60)), "18:00",
                                   "OLD60", dbm.now_iso(), dbm.now_iso()))
    r = post_json(c, "/api/reserve", {"date": jiso(today + timedelta(days=1)), "time": "16:00",
                                     "idempotency_key": "gap"})
    assert r.status_code == 200
    assert r.get_json()["gap_days"] == 61
    msgs = [m["event"] for m in conn.execute(
        "SELECT event FROM sms_messages WHERE patient_id=? ORDER BY id", (pid,)).fetchall()]
    assert "welcome_back" in msgs and msgs.index("welcome_back") < msgs.index("booked")
    body = conn.execute("SELECT body FROM sms_messages WHERE event='welcome_back'").fetchone()["body"]
    assert "دوباره آسا فیزیو را انتخاب کردید" in body and "پرونده‌ی قبلی شما نزد ماست" in body


@check("بیمار هفتگی پیامک تشکر نمی‌گیرد")
def _t23():
    c = client()
    register(c)
    conn = dbm.db()
    pid = conn.execute("SELECT id FROM patients").fetchone()["id"]
    today = settings.now().date()
    conn.execute("INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, kind,"
                 " status, tracking_code, created_at, updated_at) VALUES(?,?,?,1,'initial',"
                 " 'done',?,?,?)", (pid, jiso(today - timedelta(days=7)), "18:00",
                                   "WK7", dbm.now_iso(), dbm.now_iso()))
    post_json(c, "/api/reserve", {"date": jiso(today + timedelta(days=1)), "time": "16:00",
                                 "idempotency_key": "wk"})
    assert conn.execute("SELECT COUNT(*) c FROM sms_messages WHERE event='welcome_back'"
                        ).fetchone()["c"] == 0


# ─────────────────────────────── پیامک، وبهوک، سقف هزینه
@check("وبهوک inbound: بدون رمز ⇒ ۴۰۳ · «۱» ⇒ coming · «۲» ⇒ نه")
def _t24():
    c = client()
    register(c)
    day = next_open_day(None)
    post_json(c, "/api/reserve", {"date": day, "time": "21:00", "idempotency_key": "k"})
    phone = dbm.db().execute("SELECT mobile FROM patients").fetchone()["mobile"]
    bad = c.post("/api/sms/inbound", json={"phone": phone, "body": "1"})
    assert bad.status_code == 403, bad.status_code
    yes = c.post("/api/sms/inbound", json={"phone": phone, "body": "۱"},
                 headers={"X-SMS-Secret": settings.SMS_INBOUND_SECRET})
    assert yes.status_code == 202 and yes.get_json()["normalized"] == "yes"
    st = dbm.db().execute("SELECT status FROM appointments ORDER BY id DESC LIMIT 1").fetchone()["status"]
    assert st == "coming", st
    admin = client(); admin_login(admin)
    html = admin.get("/admin").get_data(as_text=True)
    assert "حضور قطعی ✓" in html and "tile-coming" in html


@check("متن نامشخص در پاسخ پیامک: ثبت می‌شود ولی وضعیت عوض نمی‌شود")
def _t25():
    c = client()
    register(c)
    day = next_open_day(None)
    post_json(c, "/api/reserve", {"date": day, "time": "21:00", "idempotency_key": "k"})
    phone = dbm.db().execute("SELECT mobile FROM patients").fetchone()["mobile"]
    r = c.post("/api/sms/inbound", json={"phone": phone, "body": "سلام، وقت بخیر"},
               headers={"X-SMS-Secret": settings.SMS_INBOUND_SECRET})
    assert r.status_code == 202 and r.get_json()["normalized"] == ""
    assert dbm.db().execute("SELECT status FROM appointments ORDER BY id DESC LIMIT 1"
                           ).fetchone()["status"] == "booked"


@check("یادآوری: ادعای اتمی ⇒ دو اسکنر هم‌زمان فقط یک پیامک")
def _t26():
    fresh()
    c = client()
    register(c)
    conn = dbm.db()
    day = jiso(settings.now().date())
    hh = settings.slot_times()[-1]
    pid = conn.execute("SELECT id FROM patients").fetchone()["id"]
    conn.execute("INSERT INTO appointments(patient_id, slot_date, slot_time, cabin, status,"
                 " tracking_code, created_at, updated_at) VALUES(?,?,?,1,'booked','R1',?,?)",
                 (pid, day, hh, dbm.now_iso(), dbm.now_iso()))
    # ساعت نوبت را «۱۰۰ دقیقه بعد» می‌گذاریم تا داخل پنجره‌ی یادآوری بیفتد
    now = settings.now()
    target = now + timedelta(minutes=100)
    conn.execute("UPDATE appointments SET slot_date=?, slot_time=? WHERE tracking_code='R1'",
                 (jiso(target.date()), target.strftime("%H:%M")))
    aid = conn.execute("SELECT id FROM appointments WHERE tracking_code='R1'").fetchone()["id"]
    # الف) ادعای اتمی: دو اسکنر هم‌زمان، فقط یکی برنده می‌شود
    claims = []
    barrier = threading.Barrier(2)

    def racer():
        cc = dbm.db()                       # اتصالِ مخصوص این رشته
        barrier.wait(20)
        claims.append(reminders.claim(cc, aid))

    ths = [threading.Thread(target=racer) for _ in range(2)]
    [t.start() for t in ths]
    [t.join(20) for t in ths]
    assert sorted(claims) == [False, True], claims

    # ب) اسکنِ مکرر نباید پیامک دوم بفرستد؛ و پس از شکستِ ارسال، تلاشِ بعد بماند
    conn.execute("UPDATE appointments SET confirm_sent_at = NULL, reminder_attempts = 0"
                 " WHERE id = ?", (aid,))
    before = conn.execute(
        "SELECT COUNT(*) c FROM sms_messages WHERE event='confirm'").fetchone()["c"]
    one = reminders.scan_once("test-a")
    two = reminders.scan_once("test-b")
    assert one["sent"] == 1, one
    assert two["sent"] == 0 and two["skipped"] == 0, two
    after = conn.execute("SELECT COUNT(*) c FROM sms_messages WHERE event='confirm'").fetchone()["c"]
    assert after - before == 1, (one, two, after)
    conn.execute("UPDATE appointments SET confirm_sent_at = NULL, reminder_attempts = 0"
                 " WHERE id = ?", (aid,))
    assert reminders.scan_once("test-c")["sent"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM sms_messages WHERE event='confirm'"
                        ).fetchone()["c"] == 2
    txt = conn.execute("SELECT body FROM sms_messages WHERE event='confirm'").fetchone()["body"]
    assert "عدد 1 را ارسال کنید" in txt


@check("سقف ماهانه: پیامکِ اضافه رد می‌شود و cost_events رشد نمی‌کند")
def _t27():
    c = client()
    register(c)
    conn = dbm.db()
    with dbm.tx(immediate=True):
        conn.execute("INSERT INTO cost_events(kind, amount_usd, ref, created_at) VALUES('sms',?,?,?)",
                     (settings.SMS_BUDGET_MONTHLY_USD, "fill", dbm.now_iso()))
    before_cost = dbm.month_cost_usd(conn)
    r = post_json(c, "/api/reserve", {"date": next_open_day(None), "time": "16:00",
                                     "idempotency_key": "k"})
    assert r.status_code == 200, "رزرو باید موفق بماند"
    m = conn.execute("SELECT status, error FROM sms_messages WHERE event='booked'"
                     " ORDER BY id DESC LIMIT 1").fetchone()
    assert m["status"] == "skipped_budget", dict(m)
    assert dbm.month_cost_usd(conn) == before_cost


@check("SMS_PROVIDER=file در production ⇒ برنامه بالا نمی‌آید")
def _t28():
    settings.APP_ENV = "production"
    settings.SMS_PROVIDER = "file"
    try:
        raised = False
        try:
            smsm.assert_provider()
        except SystemExit:
            raised = True
        assert raised, "باید SystemExit می‌داد"
        settings.SMS_PROVIDER = "kavenegar"
        smsm.assert_provider()
    finally:
        settings.APP_ENV = "testing"
        settings.SMS_PROVIDER = "file"


# ─────────────────────────────── امنیت داده
@check("عکس دارو و MRI از مسیر عمومی سرو نمی‌شوند؛ فقط برای کارکنان")
def _t29():
    c = client()
    register(c)
    anon = client()
    for url in ("/api/admin/patients/1/photo", "/api/admin/patients/1/mri",
                "/api/admin/patients/1", "/api/admin/slot-grid"):
        assert anon.get(url).status_code in (401, 403, 302), url
    assert anon.get("/patients/1/photo").status_code == 404
    adm = client()
    admin_login(adm)
    assert adm.get("/api/admin/patients/1/photo").status_code in (200, 404)
    r = adm.get("/api/admin/slot-grid")
    assert r.status_code == 200 and r.get_json()["grids"]


@check("magic bytes: یک .png که در واقع اجراشدنی است رد می‌شود")
def _t30():
    c = client()
    c.get("/booking/register")
    csrf = csrf_of(c)
    import io
    evil = b"\x7fELF\x02\x01\x01" + b"\x00" * 100
    data = {"full_name": "تست فایل", "national_code": GOOD_NC, "birth_year": "1365",
            "birth_month": "2", "birth_day": "15", "mobile": "09121112233", "csrf_token": csrf}
    r = c.post("/booking/register", data=data,
               content_type="multipart/form-data",
               buffered=True, query_string="")
    # ارسال واقعی با فایل:
    r = c.post("/booking/register", data={**data, "meds_photo": (io.BytesIO(evil), "virus.png")},
               content_type="multipart/form-data")
    assert r.status_code == 415, r.get_data(as_text=True)[:200]
    ok = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r2 = c.post("/booking/register", data={**data, "meds_photo": (io.BytesIO(ok), "meds.png")},
                content_type="multipart/form-data")
    assert r2.status_code in (200, 302), r2.status_code
    stored = dbm.db().execute("SELECT meds_photo, meds_mime FROM patients").fetchone()
    assert stored["meds_photo"].endswith(".png") and stored["meds_mime"] == "image/png"
    assert "virus" not in stored["meds_photo"]        # نام از محتوا، نه از نام کاربر


@check("upload_path هر مسیری را که از پوشه بیرون بزند رد می‌کند")
def _t31():
    tmp = fresh()
    ok, _m, full = upload_path(settings.UPLOAD_DIR, "../../../etc/passwd")
    assert ok is False or str(tmp) in str(full)
    ok2, _m2, full2 = upload_path(settings.UPLOAD_DIR, "sub/../../escape.png")
    assert ok2 is False or "escape.png" in str(full2) and str(tmp) in str(full2)


@check("در لاگ‌ها شماره و کد ملی فقط اثرانگشت‌اند")
def _t32():
    import io
    import logging
    lines = []
    h = logging.Handler()
    h.emit = lambda rec: lines.append(rec.getMessage())
    logging.getLogger("asa").addHandler(h)
    logging.getLogger("asa.app").addHandler(h)
    logging.getLogger("asa.sms").addHandler(h)
    c = client()
    register(c)
    dbm.db().execute("UPDATE patients SET national_code=?", (GOOD_NC,))
    c.get("/booking/logout")
    c.post("/booking/login", data={"national_code": "0345998981", "birth_year": "1399",
                                   "birth_month": "1", "birth_day": "1"})
    joined = "\n".join(lines)
    assert GOOD_NC not in joined and "09121112233" not in joined, joined
    if joined:
        assert "fp_" in joined


@check("CSRF: بدون توکن ⇒ ۴۰۳")
def _t33():
    c = client()
    register(c)
    day = next_open_day(None)
    r = c.post("/api/reserve", json={"date": day, "time": "16:00"},
               headers={})   # بدون کوکی csrf و بدون هدر
    assert r.status_code == 403, r.status_code


# ─────────────────────────────── برانگشتنِ برنامه
@check("برنامه بالا می‌آید، مسیرهای بیمار و پنل پاسخ می‌دهند")
def _t34():
    fresh()
    from api.app import create_app
    create_app(start_thread=False)
    c = client()
    for path, code in [("/", 200), ("/booking", 200), ("/booking/register", 200),
                       ("/booking/login", 200), ("/booking/reserve", 302),
                       ("/booking/done", 200), ("/admin", 302), ("/healthz", 200),
                       ("/static/app.css", 200), ("/static/admin.js", 200)]:
        r = c.get(path)
        assert r.status_code == code, (path, r.status_code)
    assert b"Vazirmatn" in c.get("/static/app.css").data
    assert "ظرفیت" in c.get("/").get_data(as_text=True)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("_t") or k.startswith("test_")]
    tests = [t for t in tests if callable(t) and getattr(t, "__name__", "").startswith(
        ("_t", "test_"))]
    print(f"اجرای {len(tests)} آزمون…")
    for t in tests:
        t()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passes")
    if bad:
        print("FAILED:", ", ".join(n for n, _ok, _e in bad))
        sys.exit(1)
