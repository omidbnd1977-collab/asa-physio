"""آسا فیزیو — سامانه‌ی نوبت‌دهی (پیاده‌سازی BOOKING.md).

بیمار : / → /booking → register|login → /booking/reserve → /booking/done
پنل   : /admin  (ورود /admin/login — کاربر/رمز در تنظیمات)
API   : /api/… ، وبهوک پیامک: POST /api/sms/inbound
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, abort, jsonify, make_response, redirect, render_template,
                   request, send_file, session, url_for)

from . import appointments as appts
from . import body_lists
from . import db as dbm
from . import policies
from . import reminders
from . import sms as smsm
from .config import ROOT, settings
from .jalali import date_range, jalali_of, to_persian_digits
from .seed import seed_if_empty
from .security import (BUCKET_LABELS, check_csrf, limiter, make_admin_token,
                       mobile_display, normalize_mobile, parse_jalali_date, save_blob,
                       sign, sniff_magic, content_filename, upload_path,
                       validate_national_code, validate_url, verify_admin_token)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("asa.app")

app = Flask(__name__, static_folder=str(ROOT / "static"),
            template_folder=str(ROOT / "templates"))
app.secret_key = settings.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = settings.UPLOAD_MAX_BYTES + 64 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = settings.COOKIE_SAMESITE
app.config["SESSION_COOKIE_SECURE"] = settings.COOKIE_SECURE
app.config["PERMANENT_SESSION_LIFETIME"] = settings.PATIENT_SESSION_HOURS * 3600
app.config["TRUSTED_PROXY"] = True

STATUS_LABELS = {"booked": "رزرو شده", "coming": "حضور قطعی ✓", "done": "انجام شد",
                 "cancelled": "لغو شده", "no_show": "مراجعه نشد"}
KIND_LABELS = {"initial": "نخستین", "reschedule": "جابه‌جایی", "followup": "جلسه‌ی بعدی"}


# ------------------------------------------------------------------ کمک‌ها
def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    return "application/json" in (request.headers.get("Accept") or "")


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def current_patient():
    pid = session.get("patient_id")
    if not pid:
        return None
    row = dbm.db().execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def is_admin() -> bool:
    token, sig = session.get("admin_token"), session.get("admin_sig")
    if not token or not sig or not verify_admin_token(token, sig):
        if token or sig:
            session.pop("admin_token", None)
            session.pop("admin_sig", None)
        return False
    return True


def ctx(**extra) -> dict:
    base = dict(
        clinic=settings.CLINIC_NAME, address=settings.ADDRESS,
        phone_disp=to_persian_digits(settings.PHONE), env=settings.APP_ENV,
        slot_times=[to_persian_digits(t) for t in settings.slot_times()],
        patient=current_patient(), is_admin=is_admin(),
        status_labels=STATUS_LABELS, kind_labels=KIND_LABELS,
        fa=to_persian_digits, now_jalali=jalali_of(settings.now().date()),
        csrf=csrf_token(), few_threshold=settings.FEW_LEFT_THRESHOLD,
        cabins=settings.CABINS, lead_min=appts.MIN_LEAD_MIN,
    )
    base.update(extra)
    return base


def data() -> dict:
    d = dict(request.get_json(silent=True) or {}) if request.is_json else {}
    for k, v in request.form.items():
        d.setdefault(k, v)
    return d


def err(status: int, code: str, message: str):
    if wants_json():
        return jsonify({"error": code, "message": message}), status
    return (render_template("error.jinja", message=message, code=code, status=status,
                           **ctx()), status)


def client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "unknown")


def limited(bucket: str, key: str):
    ok, reset, _used = limiter.hit(bucket, key)
    if ok:
        return None
    return jsonify({"error": "rate_limited", "bucket": bucket,
                    "limit": limiter.limit_for(bucket), "retry_in": reset,
                    "message": f"سقف «{BUCKET_LABELS.get(bucket, bucket)}» پر شده؛ "
                               f"{to_persian_digits(max(reset, 1))} ثانیه دیگر."}), 429


def patient_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("patient_id"):
            if wants_json():
                return jsonify({"error": "auth_required", "message": "اول وارد شوید."}), 401
            return redirect(url_for("booking_login"))
        return fn(*a, **kw)
    return wrapper


def staff_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            if wants_json():
                return jsonify({"error": "staff_only", "message": "فقط برای کارکنان."}), 403
            return redirect(url_for("admin_login"))
        return fn(*a, **kw)
    return wrapper


def csrf_protected(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        ok, msg = check_csrf(request, session.get("csrf"))
        if not ok:
            return err(403, "csrf", msg)
        return fn(*a, **kw)
    return wrapper


@app.after_request
def add_headers(resp):
    """کوکی که فرم‌ها لازم دارند: asa_csrf (double-submit، خوانده‌شدنی برای JS)."""
    token = session.get("csrf")
    if token and request.cookies.get(settings.CSRF_COOKIE_NAME) != token:
        resp.set_cookie(settings.CSRF_COOKIE_NAME, token, httponly=False,
                        samesite=settings.COOKIE_SAMESITE, secure=settings.COOKIE_SECURE,
                        path="/", max_age=settings.PATIENT_SESSION_HOURS * 3600)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    # X-Frame-Options: DENY فقط در تولید — وگرنه پیش‌نمایشِ iframe-محور باز نمی‌شود.
    if settings.APP_ENV == "production":
        resp.headers.setdefault("X-Frame-Options", "DENY")
    if request.path.startswith("/booking") or request.path.startswith("/admin"):
        resp.headers.setdefault("Cache-Control", "private, no-store")
    return resp


# ------------------------------------------------------------------ سایت / درخواست سریع
@app.get("/")
def home():
    conn = dbm.db()
    stats = {
        "patients": conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"],
        "done": conn.execute("SELECT COUNT(*) c FROM appointments WHERE status='done'"
                             ).fetchone()["c"],
        "capacity": settings.CABINS * settings.CABIN_CAPACITY * len(settings.slot_times()),
        "open_today": any(not s["disabled"]
                          for s in appts.slot_grid(conn, appts.jdate_today())["slots"]),
    }
    return render_template("home.jinja", stats=stats, **ctx())


@app.post("/contact")
@csrf_protected
def contact():
    d = data()
    name = (d.get("name") or "").strip()
    if len(name) < 2:
        return err(400, "bad_name", "نام را بنویسید.")
    ok, msg, mobile = normalize_mobile(d.get("phone") or "")
    if not ok:
        return err(400, "bad_phone", msg)
    message = (d.get("message") or "").strip()
    if len(message) < 5:
        return err(400, "short_message", "پیام خیلی کوتاه است.")
    with dbm.tx(immediate=True):
        dbm.db().execute(
            "INSERT INTO site_requests(name, phone, topic, message, source, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (name, mobile, (d.get("topic") or "عمومی").strip()[:60], message[:2000],
             "site quick form", dbm.now_iso()))
    if wants_json():
        return jsonify({"ok": True, "message": "درخواست شما ثبت شد."}), 201
    return render_template("contact_done.jinja", **ctx())


# ------------------------------------------------------------------ شروع مسیر بیمار
@app.get("/booking")
def booking_root():
    return render_template("start.jinja", **ctx())


@app.route("/booking/register", methods=["GET", "POST"])
def booking_register():
    if request.method == "GET":
        return render_template("register.jinja", **ctx(values={}))
    return do_register()


def do_register():
    d = data()
    blocked = limited("register_ip", client_ip())
    if blocked:
        return blocked

    name = " ".join((d.get("full_name") or "").split())
    if len(name.split()) < 2:
        return err(400, "bad_name", "نام و نام خانوادگی لازم است (حداقل دو کلمه).")
    ok, msg, code = validate_national_code(d.get("national_code") or "")
    if not ok:
        return err(400, "bad_national_code", msg)
    blocked = limited("register_code", code)
    if blocked:
        return blocked
    ok, msg, jd = parse_jalali_date(d.get("birth_year"), d.get("birth_month"),
                                    d.get("birth_day"))
    if not ok:
        return err(400, "bad_birthdate", msg)
    ok, msg, mobile = normalize_mobile(d.get("mobile") or "")
    if not ok:
        return err(400, "bad_mobile", msg)
    mri_url = (d.get("mri_url") or "").strip()
    if mri_url:
        ok, msg = validate_url(mri_url)
        if not ok:
            return err(400, "bad_mri_url", msg)

    meds_name = meds_mime = None
    upload = request.files.get("meds_photo")
    if upload is not None and (upload.filename or ""):
        blob = upload.read(settings.UPLOAD_MAX_BYTES + 1)
        if len(blob) > settings.UPLOAD_MAX_BYTES:
            return err(413, "too_large", "حجم فایل بیشتر از ۵ مگابایت است.")
        ok, msg, meds_name, meds_mime = save_blob(blob, "meds")
        if not ok:
            return err(415, "bad_file", msg)

    birth = f"{jd[0]:04d}-{jd[1]:02d}-{jd[2]:02d}"
    ortho = (d.get("orthopedist") or "").strip()[:120] or None
    conn = dbm.db()
    with dbm.tx(immediate=True):
        existing = conn.execute("SELECT id FROM patients WHERE national_code = ?",
                               (code,)).fetchone()
        if existing:
            pid = int(existing["id"])
            conn.execute(
                "UPDATE patients SET full_name=?, birth_jdate=?, mobile=?, mri_url=?,"
                " orthopedist=?, meds_photo=COALESCE(?, meds_photo),"
                " meds_mime=COALESCE(?, meds_mime) WHERE id=?",
                (name, birth, mobile, mri_url or None, ortho, meds_name, meds_mime, pid))
            created = False
        else:
            conn.execute(
                "INSERT INTO patients(full_name, national_code, birth_jdate, mobile,"
                " mri_url, orthopedist, meds_photo, meds_mime, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (name, code, birth, mobile, mri_url or None, ortho, meds_name, meds_mime,
                 dbm.now_iso()))
            pid = int(conn.execute("SELECT last_insert_rowid() i").fetchone()["i"])
            dbm.bump(conn, "registrations")
            created = True
        sms = smsm.send(conn, event="registered", to=mobile, patient_id=pid,
                        body=smsm.render("registered", name=name))
    session.permanent = True
    session["patient_id"] = pid
    log.info("register %s → patient %s (created=%s, sms=%s)",
             fingerprint_code(code), pid, created, sms["status"])
    if wants_json():
        return jsonify({"ok": True, "patient_id": pid, "created": created,
                        "sms_status": sms["status"]}), (201 if created else 200)
    return redirect(url_for("booking_reserve"))


def fingerprint_code(code: str) -> str:
    from .security import fingerprint

    return fingerprint(f"nc:{code}")


@app.route("/booking/login", methods=["GET", "POST"])
def booking_login():
    if request.method == "GET":
        return render_template("login.jinja", **ctx())
    return do_login()


def do_login():
    d = data()
    blocked = limited("login_ip", client_ip())
    if blocked:
        return blocked
    ok, _msg, code = validate_national_code(d.get("national_code") or "")
    if not ok:
        blocked = limited("login_code", code or "bad")
        if blocked:
            return blocked
        limiter.hit("login_fail", "bad")
        return err(401, "bad_credentials", "کد ملی یا تاریخ تولد درست نیست.")
    blocked = limited("login_code", code)
    if blocked:
        return blocked
    if not limiter.hit("login_fail", code)[0]:
        return err(429, "locked", "چند بار اشتباه زده‌اید؛ یک ساعت صبر کنید "
                                  "یا با مطب تماس بگیرید.")
    ok, _msg, jd = parse_jalali_date(d.get("birth_year"), d.get("birth_month"),
                                    d.get("birth_day"))
    if not ok:
        limiter.hit("login_fail", code)
        return err(401, "bad_credentials", "کد ملی یا تاریخ تولد درست نیست.")
    birth = f"{jd[0]:04d}-{jd[1]:02d}-{jd[2]:02d}"
    conn = dbm.db()
    row = conn.execute("SELECT * FROM patients WHERE national_code = ? AND birth_jdate = ?",
                       (code, birth)).fetchone()
    if not row:
        limiter.hit("login_fail", code)
        log.info("login denied for %s", fingerprint_code(code))
        return err(401, "bad_credentials",
                   "با این کد ملی و تاریخ تولد رکوردی نیست. اگر بار اول است، ثبت‌نام کنید.")
    limiter._hits.pop(("login_fail", code), None)
    with dbm.tx(immediate=True):
        conn.execute("UPDATE patients SET last_login_at = ? WHERE id = ?",
                     (dbm.now_iso(), row["id"]))
    session.permanent = True
    session["patient_id"] = int(row["id"])
    if wants_json():
        return jsonify({"ok": True, "patient_id": int(row["id"])})
    return redirect(url_for("booking_reserve"))


@app.get("/booking/logout")
def booking_logout():
    session.pop("patient_id", None)
    return redirect(url_for("home"))


# ------------------------------------------------------------------ جدول نوبت‌ها
@app.get("/booking/reserve")
@patient_required
def booking_reserve():
    conn = dbm.db()
    today = appts.jdate_today()
    sel = request.args.get("date") or today
    if sel < today or not _valid_jdate(sel):
        sel = today
    return render_template("reserve.jinja", **ctx(
        grid=appts.slot_grid(conn, sel),
        strip=_strip(conn, today), selected=sel,
        mine=appts.patient_appointments(conn, session["patient_id"], upcoming_only=True)))


def _valid_jdate(value: str) -> bool:
    try:
        appts.parse_jdate(value)
        return True
    except appts.BookingError:
        return False


def _strip(conn, today_iso: str) -> list[dict]:
    out = []
    per_day = settings.CABINS * settings.CABIN_CAPACITY
    for day in date_range(settings.now().date(), settings.STRIP_DAYS):
        j = jalali_of(day)
        taken = conn.execute(
            "SELECT COUNT(*) c FROM appointments WHERE slot_date = ?"
            " AND status IN ('booked','coming')", (j["iso"],)).fetchone()["c"]
        total = per_day * len(settings.slot_times())
        out.append({**j, "date_val": j["iso"], "free": max(total - int(taken), 0),
                    "total": total, "is_today": j["iso"] == today_iso})
    return out


@app.get("/api/slots")
@patient_required
def api_slots():
    conn = dbm.db()
    today = appts.jdate_today()
    jdate = request.args.get("date") or today
    if jdate < today or not _valid_jdate(jdate):
        jdate = today
    return jsonify({"grid": appts.slot_grid(conn, jdate), "strip": _strip(conn, today)})


@app.post("/api/reserve")
@patient_required
@csrf_protected
def api_reserve():
    d = data()
    conn = dbm.db()
    patient = conn.execute("SELECT * FROM patients WHERE id = ?",
                          (session["patient_id"],)).fetchone()
    jdate = (d.get("date") or "").strip()
    try:
        out = appts.book(conn, patient=patient, jdate=jdate,
                        hhmm=(d.get("time") or "").strip(),
                        note=(d.get("note") or "").strip()[:400],
                        idem=(d.get("idempotency_key") or "").strip()[:64] or None)
    except appts.BookingError as exc:
        payload = {"error": exc.code, "message": exc.message}
        if exc.code == "slot_full":
            payload["refresh"] = True
        payload["grid"] = appts.slot_grid(conn, jdate if _valid_jdate(jdate)
                                          else appts.jdate_today())
        return jsonify(payload), exc.status
    appt = out["appointment"]
    resp = make_response(jsonify({"ok": True, "replayed": out["replayed"],
                                 "appointment": {**appt, "code": appt["tracking_code"]},
                                 "sms": out["sms"], "gap_days": out.get("gap_days"),
                                 "grid": appts.slot_grid(conn, jdate)}))
    resp.set_cookie("asa_track", appt["tracking_code"], httponly=True,
                    samesite=settings.COOKIE_SAMESITE, secure=settings.COOKIE_SECURE,
                    path="/booking", max_age=30 * 86400)
    return resp


@app.get("/booking/done")
def booking_done():
    code = (request.args.get("code") or request.cookies.get("asa_track") or "").strip().upper()
    conn = dbm.db()
    row = None
    if code:
        row = conn.execute("SELECT a.*, p.full_name FROM appointments a"
                           " JOIN patients p ON p.id = a.patient_id"
                           " WHERE a.tracking_code = ?", (code,)).fetchone()
    if not row:
        return render_template("done_missing.jinja", **ctx(code=code))
    appt = dict(row)
    appt["day"] = jalali_of(appts.gregorian_of(appt["slot_date"]))
    appt["time_fa"] = to_persian_digits(appt["slot_time"])
    appt["cabin_fa"] = to_persian_digits(appt["cabin"])
    appt["phone_disp"] = mobile_display(appt["mobile"]) if appt.get("mobile") else ""
    appt.pop("mobile", None)
    appt.pop("patient_id", None)
    reminder = conn.execute(
        "SELECT status, created_at FROM sms_messages WHERE appointment_id = ?"
        " AND event = 'confirm' ORDER BY id DESC LIMIT 1", (appt["id"],)).fetchone()
    return render_template("done.jinja", appt=appt, reminder=dict(reminder) if reminder else None,
                           **ctx())


@app.post("/api/appointments/cancel")
@patient_required
@csrf_protected
def api_cancel():
    d = data()
    try:
        out = appts.cancel_by_patient(dbm.db(), int(d.get("id") or 0), session["patient_id"])
        return jsonify({"ok": True, "appointment": out["appointment"]})
    except appts.BookingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status


# ------------------------------------------------------------------ پنل پزشک
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        return render_template("admin_login.jinja", **ctx())
    d = data()
    blocked = limited("login_ip", "admin:" + client_ip())
    if blocked:
        return blocked
    ok_user = hmac.compare_digest(str(d.get("user") or ""), settings.ADMIN_USER)
    ok_pass = hmac.compare_digest(str(d.get("password") or ""), settings.ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        return err(401, "bad_credentials", "نام کاربری یا رمز اشتباه است.")
    token = make_admin_token()
    session.permanent = True
    session["admin_token"] = token
    session["admin_sig"] = sign(token)
    session["admin_role"] = "physician"
    return redirect(url_for("admin_home"))


@app.get("/admin/logout")
def admin_logout():
    session.pop("admin_token", None)
    session.pop("admin_sig", None)
    session.pop("admin_role", None)
    return redirect(url_for("home"))


@app.get("/admin")
@staff_required
def admin_home():
    conn = dbm.db()
    today = appts.jdate_today()
    rows = conn.execute(
        "SELECT a.*, p.full_name, p.mobile, p.meds_photo, p.mri_file, p.mri_url,"
        " p.note AS patient_note FROM appointments a JOIN patients p ON p.id = a.patient_id"
        " ORDER BY a.slot_date DESC, a.slot_time DESC, a.cabin LIMIT 500").fetchall()
    groups: dict[str, list] = {}
    for r in rows:
        d = dict(r)
        d["day"] = jalali_of(appts.gregorian_of(d["slot_date"]))
        d["time_fa"] = to_persian_digits(d["slot_time"])
        d["cabin_fa"] = to_persian_digits(d["cabin"])
        d["phone_disp"] = mobile_display(d["mobile"])
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["kind_label"] = KIND_LABELS.get(d["kind"], d["kind"])
        d["is_today"] = d["slot_date"] == today
        d["has_mri"] = bool(d["mri_file"] or d["mri_url"])
        d.pop("mobile", None)
        groups.setdefault(d["slot_date"], []).append(d)
    days = [{"jdate": k, "day": jalali_of(appts.gregorian_of(k)), "rows": v}
            for k, v in sorted(groups.items(), reverse=True)]

    patients = []
    for p in conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM appointments a WHERE a.patient_id = p.id)"
            " AS appt_count, (SELECT COUNT(*) FROM patient_sessions s"
            " WHERE s.patient_id = p.id) AS session_count,"
            " (SELECT MAX(session_date) FROM patient_sessions s2"
            " WHERE s2.patient_id = p.id) AS last_session"
            " FROM patients p ORDER BY p.created_at DESC LIMIT 400").fetchall():
        d = dict(p)
        d["phone_disp"] = mobile_display(d["mobile"])
        d["birth"] = jalali_of(appts.gregorian_of(d["birth_jdate"]))
        d["nc_fp"] = fingerprint_code(d.pop("national_code"))
        d["last_session_j"] = jalali_of(appts.gregorian_of(d["last_session"])) \
            if d.get("last_session") else None
        patients.append(d)

    sms_rows = []
    for m in conn.execute("SELECT s.*, p.full_name FROM sms_messages s LEFT JOIN patients p"
                          " ON p.id = s.patient_id ORDER BY s.id DESC LIMIT 250").fetchall():
        d = dict(m)
        d["to_disp"] = mobile_display(d["to_phone"])
        d["event_label"] = smsm.EVENT_LABELS.get(d["event"], d["event"])
        sms_rows.append(d)
    inbound = [dict(x) for x in conn.execute(
        "SELECT i.*, p.full_name FROM sms_inbound i LEFT JOIN patients p ON p.mobile = i.phone"
        " ORDER BY i.id DESC LIMIT 60").fetchall()]
    site_reqs = []
    for x in conn.execute("SELECT * FROM site_requests ORDER BY id DESC LIMIT 120").fetchall():
        d = dict(x)
        d["phone_disp"] = mobile_display(d.get("phone") or "")
        site_reqs.append(d)

    def scalar(sql, *a):
        return int(conn.execute(sql, a).fetchone()[0])

    cnt = conn.execute("SELECT value FROM counters WHERE name='sessions_done'").fetchone()
    stats = {
        "today": scalar("SELECT COUNT(*) FROM appointments WHERE slot_date = ?"
                        " AND status IN ('booked','coming','done')", today),
        "coming": scalar("SELECT COUNT(*) FROM appointments WHERE status='coming'"
                         " AND slot_date >= ?", today),
        "done": scalar("SELECT COUNT(*) FROM appointments WHERE status='done'"),
        "sessions_counter": int(cnt["value"]) if cnt else 0,
        "patients": scalar("SELECT COUNT(*) FROM patients"),
        "sms_sent": scalar("SELECT COUNT(*) FROM sms_messages WHERE status='sent'"),
        "sms_bad": scalar("SELECT COUNT(*) FROM sms_messages WHERE status IN"
                          " ('failed','skipped_budget')"),
        "requests_open": scalar("SELECT COUNT(*) FROM site_requests WHERE handled = 0"),
        "month_cost": round(dbm.month_cost_usd(conn), 3),
        "budget": settings.SMS_BUDGET_MONTHLY_USD,
        "capacity_day": settings.CABINS * settings.CABIN_CAPACITY * len(settings.slot_times()),
    }
    systems = {
        "provider": settings.SMS_PROVIDER,
        "lead": settings.REMINDER_LEAD_MIN,
        "interval": settings.REMINDER_INTERVAL_SEC,
        "tz": settings.TIMEZONE,
        "server_now": jalali_of(settings.now().date())["long"] + "  " +
                      to_persian_digits(settings.now().strftime("%H:%M:%S")),
        "limits": [{"bucket": k, "label": BUCKET_LABELS.get(k, k),
                    "limit": limiter.limit_for(k)}
                   for k in ("register_ip", "register_code", "login_ip", "login_code",
                             "reserve_day", "inbound_min")],
        "live": limiter.snapshot(),
        "db_path": str(settings.DB_PATH),
        "upload_dir": str(settings.UPLOAD_DIR),
        "sms_log": str(settings.SMS_LOG_PATH),
        "cookie": f"HttpOnly + SameSite={settings.COOKIE_SAMESITE} · "
                  f"{settings.PATIENT_SESSION_HOURS} ساعت",
        "slot_math": f"{settings.WORK_START} → {settings.WORK_END} · "
                     f"{len(settings.slot_times())} خانه × {settings.CABINS} کابین × "
                     f"{settings.CABIN_CAPACITY} = {stats['capacity_day']} نوبت",
        "prod_block": "production + SMS_PROVIDER=file → بالا نمی‌آید",
        "last_scan": reminders.last_scan,
    }
    return render_template("admin.jinja", days=days, patients=patients, sms_rows=sms_rows,
                           inbound=inbound, site_reqs=site_reqs, stats=stats, systems=systems,
                           body_parts=body_lists.fetch_grouped(conn),
                           treatments=body_lists.fetch_treatments(conn), **ctx())


@app.post("/api/admin/appointments/<int:aid>/status")
@staff_required
@csrf_protected
def api_admin_status(aid: int):
    d = data()
    ok, msg = policies.authorize("appointments", "write", "physician")
    if not ok:
        return err(403, "policy", msg)
    try:
        out = appts.set_status(dbm.db(), aid, d.get("status") or "",
                               send_sms=str(d.get("sms", "")).lower() in {"1", "true", "yes"})
        return jsonify({"ok": True, "appointment": out["appointment"]})
    except appts.BookingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status


@app.post("/api/admin/appointments/<int:aid>/reschedule")
@staff_required
@csrf_protected
def api_admin_reschedule(aid: int):
    d = data()
    conn = dbm.db()
    try:
        out = appts.reschedule(conn, aid, (d.get("date") or "").strip(),
                              (d.get("time") or "").strip())
        return jsonify({"ok": True, "appointment": out["appointment"],
                        "unchanged": bool(out.get("unchanged")),
                        "grid": appts.slot_grid(conn, d.get("date") or appts.jdate_today())})
    except appts.BookingError as exc:
        after = conn.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
        return jsonify({"error": exc.code, "message": exc.message,
                        "original": dict(after) if after else None}), exc.status


@app.post("/api/admin/appointments/<int:aid>/followup")
@staff_required
@csrf_protected
def api_admin_followup(aid: int):
    d = data()
    conn = dbm.db()
    appt = conn.execute("SELECT * FROM appointments WHERE id = ?", (aid,)).fetchone()
    if not appt:
        return err(404, "not_found", "نوبت پیدا نشد.")
    patient = conn.execute("SELECT * FROM patients WHERE id = ?", (appt["patient_id"],)).fetchone()
    jdate, hhmm = (d.get("date") or "").strip(), (d.get("time") or "").strip()
    try:
        out = appts.book(conn, patient=patient, jdate=jdate, hhmm=hhmm, kind="followup",
                        note=(d.get("note") or "").strip()[:400], enforce_daily_limit=False)
        with dbm.tx(immediate=True):
            smsm.send(conn, event="followup", to=patient["mobile"],
                      patient_id=patient["id"], appointment_id=out["appointment"]["id"],
                      body=smsm.render("followup", name=patient["full_name"],
                                       jdate=jalali_of(appts.gregorian_of(jdate))["long"],
                                       time=to_persian_digits(hhmm),
                                       cabin=to_persian_digits(out["appointment"]["cabin"]),
                                       code=out["appointment"]["tracking_code"]))
        return jsonify({"ok": True, "appointment": out["appointment"]})
    except appts.BookingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status


@app.post("/api/admin/reminder")
@staff_required
@csrf_protected
def api_admin_reminder():
    """دکمه‌ی «ارسال یادآوری» = همان پیامک تأیید حضور، دستی."""
    d = data()
    conn = dbm.db()
    appt = conn.execute("SELECT a.*, p.full_name, p.mobile FROM appointments a"
                        " JOIN patients p ON p.id = a.patient_id WHERE a.id = ?",
                        (int(d.get("id") or 0),)).fetchone()
    if not appt:
        return err(404, "not_found", "نوبت پیدا نشد.")
    res = smsm.send(conn, event="confirm", to=appt["mobile"], patient_id=appt["patient_id"],
                    appointment_id=appt["id"],
                    body=smsm.render("confirm", name=appt["full_name"],
                                     jdate=jalali_of(appts.gregorian_of(appt["slot_date"]))["long"],
                                     time=to_persian_digits(appt["slot_time"])),
                    allow_over_budget=True)
    return jsonify({"ok": True, "status": res["status"], "error": res["error"] or None})


@app.get("/api/admin/slot-grid")
@staff_required
def api_admin_slot_grid():
    conn = dbm.db()
    today = appts.jdate_today()
    start = (request.args.get("date") or today).strip()
    if not _valid_jdate(start):
        start = today
    try:
        days = max(1, min(int(request.args.get("days") or 1), 30))
    except ValueError:
        days = 1
    greg = appts.gregorian_of(start)
    grids = [appts.slot_grid(conn, jalali_of(d)["iso"]) for d in date_range(greg, days)]
    return jsonify({"grids": grids})


@app.post("/api/admin/sessions")
@staff_required
@csrf_protected
def api_admin_session():
    d = data()
    ok, msg = policies.authorize("patient_sessions", "write", "physician")
    if not ok:
        return err(403, "policy", msg)
    patient_id = int(d.get("patient_id") or 0)
    if not patient_id:
        return err(400, "bad_patient", "بیمار مشخص نیست.")

    def lst(key):
        v = d.get(key)
        if isinstance(v, list):
            return [str(x) for x in v]
        return [x for x in str(v or "").split("|") if x.strip()]

    def maybe_int(v):
        from .security import latinize_digits

        try:
            n = int(latinize_digits(str(v)).strip())
            return n if 0 <= n <= 10 else None
        except (TypeError, ValueError):
            return None

    try:
        out = appts.record_session(
            dbm.db(), patient_id=patient_id,
            appointment_id=int(d["appointment_id"]) if d.get("appointment_id") else None,
            jdate=(d.get("date") or appts.jdate_today()),
            parts=lst("parts"), treatments=lst("treatments"),
            pain_before=maybe_int(d.get("pain_before")),
            pain_after=maybe_int(d.get("pain_after")),
            findings=(d.get("findings") or "").strip()[:2000],
            next_plan=(d.get("next_plan") or "").strip()[:2000],
            add_parts=lst("add_parts"), add_treatments=lst("add_treatments"),
            notify=str(d.get("notify", "")).lower() in {"1", "true", "yes"})
        return jsonify({"ok": True, "session_id": out["session_id"],
                        "appointment_done": out["appointment_done"], "sms": out["sms"]})
    except appts.BookingError as exc:
        return jsonify({"error": exc.code, "message": exc.message}), exc.status


@app.post("/api/admin/patients/<int:pid>/note")
@staff_required
@csrf_protected
def api_admin_note(pid: int):
    d = data()
    ok, msg = policies.authorize("patients", "write", "physician")
    if not ok:
        return err(403, "policy", msg)
    with dbm.tx(immediate=True):
        note = (d.get("note") or "").strip()[:4000]
        dbm.db().execute("UPDATE patients SET note = ? WHERE id = ?", (note or None, pid))
    return jsonify({"ok": True})


@app.post("/api/admin/patients/<int:pid>/mri")
@staff_required
@csrf_protected
def api_admin_mri(pid: int):
    """پزشک می‌تواند فایل MRI را اضافه یا جایگزین کند."""
    upload = request.files.get("mri")
    if upload is None or not (upload.filename or ""):
        return err(400, "no_file", "فایل نیامد.")
    blob = upload.read(settings.UPLOAD_MAX_BYTES + 1)
    if len(blob) > settings.UPLOAD_MAX_BYTES:
        return err(413, "too_large", "حجم فایل بیشتر از ۵ مگابایت است.")
    ok, msg, _mime, name = save_blob(blob, "mri")
    if not ok:
        return err(415, "bad_file", msg)
    mime = (sniff_magic(blob[:16]) or ("application/octet-stream",))[0]
    with dbm.tx(immediate=True):
        dbm.db().execute("UPDATE patients SET mri_file = ?, mri_mime = ? WHERE id = ?",
                         (name, mime, pid))
    return jsonify({"ok": True, "file": name})


@app.get("/api/admin/patients/<int:pid>/photo")
@staff_required
def api_admin_photo(pid: int):
    """عکس داروها — هرگز از مسیر عمومی سرو نمی‌شود."""
    row = dbm.db().execute("SELECT meds_photo, meds_mime FROM patients WHERE id = ?",
                           (pid,)).fetchone()
    if not row or not row["meds_photo"]:
        abort(404)
    return _private_file(row["meds_photo"], row["meds_mime"])


@app.get("/api/admin/patients/<int:pid>/mri")
@staff_required
def api_admin_mri_file(pid: int):
    row = dbm.db().execute("SELECT mri_file, mri_mime FROM patients WHERE id = ?",
                           (pid,)).fetchone()
    if not row or not row["mri_file"]:
        abort(404)
    return _private_file(row["mri_file"], row["mri_mime"])


def _private_file(name: str, mime):
    ok, _msg, full = upload_path(settings.UPLOAD_DIR, os.path.basename(name or ""))
    if not ok or not full or not os.path.exists(full):
        abort(404)
    sniffed = sniff_magic(open(full, "rb").read(16))
    resp = make_response(send_file(full, mimetype=(sniffed[0] if sniffed else
                                                   (mime or "application/octet-stream"))))
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Disposition"] = "inline"
    return resp


@app.get("/api/admin/patients/<int:pid>")
@staff_required
def api_admin_patient(pid: int):
    conn = dbm.db()
    row = conn.execute("SELECT * FROM patients WHERE id = ?", (pid,)).fetchone()
    if not row:
        abort(404)
    p = dict(row)
    p["phone_disp"] = mobile_display(p["mobile"])
    p["birth"] = jalali_of(appts.gregorian_of(p["birth_jdate"]))
    p["nc_fp"] = fingerprint_code(p.pop("national_code"))
    return jsonify({"patient": p,
                    "appointments": appts.patient_appointments(conn, pid),
                    "timeline": appts.timeline(conn, pid)})


@app.post("/api/admin/items")
@staff_required
@csrf_protected
def api_admin_items():
    d = data()
    table = "body_parts" if d.get("kind") == "part" else "treatments"
    ok, msg = policies.authorize(table, "write", "physician")
    if not ok:
        return err(403, "policy", msg)
    label = " ".join((d.get("label") or "").split())
    if len(label) < 2:
        return err(400, "short_label", "نام کوتاه است.")
    conn = dbm.db()
    with dbm.tx(immediate=True):
        if conn.execute(f"SELECT id FROM {table} WHERE label = ?", (label,)).fetchone():
            return err(409, "duplicate", "این مورد از قبل در فهرست است.")
        if table == "body_parts":
            conn.execute("INSERT INTO body_parts(label, grp, source, created_at)"
                         " VALUES(?, 'سایر', 'physician', ?)", (label, dbm.now_iso()))
        else:
            conn.execute("INSERT INTO treatments(label, source, created_at)"
                         " VALUES(?, 'physician', ?)", (label, dbm.now_iso()))
    return jsonify({"ok": True, "items": body_lists.list_of(conn, table)})


@app.post("/api/admin/requests/<int:rid>/handle")
@staff_required
@csrf_protected
def api_admin_handle(rid: int):
    with dbm.tx(immediate=True):
        dbm.db().execute("UPDATE site_requests SET handled = 1 - handled WHERE id = ?", (rid,))
    return jsonify({"ok": True})


@app.get("/api/track/<code>")
def api_track(code: str):
    row = dbm.db().execute("SELECT id FROM appointments WHERE tracking_code = ?",
                           (code.strip().upper(),)).fetchone()
    return jsonify({"found": bool(row)}), (200 if row else 404)


# ------------------------------------------------------------------ وبهوک پیامک
@app.post("/api/sms/inbound")
def api_sms_inbound():
    if not hmac.compare_digest(request.headers.get("X-SMS-Secret", ""),
                              settings.SMS_INBOUND_SECRET):
        return jsonify({"error": "forbidden", "message": "X-SMS-Secret نمی‌خواند."}), 403
    blocked = limited("inbound_min", client_ip())
    if blocked:
        return blocked
    d = request.get_json(silent=True) or {}
    ok, _msg, mobile = normalize_mobile(d.get("phone") or "")
    body = str(d.get("body") or "").strip()
    if not ok or not body:
        return jsonify({"error": "bad_payload", "message": "phone و body لازم است."}), 400
    verdict = "yes" if smsm.looks_yes(body) else ("no" if smsm.looks_no(body) else "")
    conn = dbm.db()
    today = appts.jdate_today()
    with dbm.tx(immediate=True):
        conn.execute("INSERT INTO sms_inbound(phone, body, normalized, created_at)"
                     " VALUES(?,?,?,?)", (mobile, body[:480], verdict or None, dbm.now_iso()))
        inbound_id = int(conn.execute("SELECT last_insert_rowid() i").fetchone()["i"])
        matched = None
        if verdict:
            row = conn.execute(
                "SELECT a.id FROM appointments a JOIN patients p ON p.id = a.patient_id"
                " WHERE p.mobile = ? AND a.slot_date >= ? AND a.status IN ('booked','coming')"
                " ORDER BY a.slot_date, a.slot_time LIMIT 1", (mobile, today)).fetchone()
            if row:
                status = "coming" if verdict == "yes" else "not_coming"
                appts.apply_attendance(conn, int(row["id"]), status)
                matched = int(row["id"])
        conn.execute("UPDATE sms_inbound SET matched_id = ? WHERE id = ?", (matched, inbound_id))
    log.info("inbound %s → %s %s", fingerprint_phone(mobile), verdict or "ignored",
             f"appt {matched}" if matched else "")
    return jsonify({"ok": True, "matched": matched, "normalized": verdict}), 202


def fingerprint_phone(mobile: str) -> str:
    from .security import fingerprint

    return fingerprint(f"tel:{mobile}")


# ------------------------------------------------------------------ سلامت
@app.get("/healthz")
def healthz():
    conn = dbm.db()
    return jsonify({"ok": True, "env": settings.APP_ENV, "provider": settings.SMS_PROVIDER,
                    "tz": settings.TIMEZONE, "patients": conn.execute(
                        "SELECT COUNT(*) c FROM patients").fetchone()["c"],
                    "now": settings.now().isoformat(timespec="seconds")})


@app.errorhandler(404)
def not_found(_e):
    if wants_json():
        return jsonify({"error": "not_found", "message": "چیزی اینجا نیست."}), 404
    return render_template("error.jinja", message="چیزی اینجا نیست.", code="not_found",
                           **ctx()), 404


@app.errorhandler(413)
def too_big(_e):
    limit_mb = settings.UPLOAD_MAX_BYTES // (1024 * 1024)
    if request.path.startswith("/booking/register"):
        return render_template("register.jinja", values=request.form.to_dict(),
                               error=f"حجم فایل بیشتر از {to_persian_digits(limit_mb)} مگابایت است.",
                               **ctx()), 413
    return jsonify({"error": "too_large",
                    "message": f"حجم فایل بیشتر از {to_persian_digits(limit_mb)} مگابایت است."}), 413


@app.errorhandler(500)
def boom(_e):
    return err(500, "internal", "خطای داخلی.")


_instance: Flask | None = None


def create_app(start_thread: bool | None = None) -> Flask:
    global _instance
    dbm.init()
    smsm.assert_provider()
    seed_if_empty(dbm.db())
    if _instance is None:
        _instance = app
    if start_thread is None:
        start_thread = os.environ.get("ASA_START_THREAD", "1") != "0"
    if start_thread:
        reminders.start_in_app_thread()
    return _instance
