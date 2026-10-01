"""تنظیمات. همه‌چیز از environment قابل تغییر است؛ پیش‌فرض‌ها == BOOKING.md."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _fl(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


class settings:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY", "asa-dev-secret-change-me")

    DATA_DIR = Path(os.environ.get("ASA_DATA_DIR", str(ROOT / "data")))
    DB_PATH = Path(os.environ.get("ASA_DB_PATH", str(DATA_DIR / "booking.db")))
    UPLOAD_DIR = Path(os.environ.get("ASA_UPLOAD_DIR", str(DATA_DIR / "uploads")))

    # --- کلینیک ---
    CLINIC_NAME = os.environ.get("ASA_CLINIC_NAME", "آسا فیزیو")
    ADDRESS = os.environ.get(
        "ASA_ADDRESS", "تهران، خیابان ولی‌عصر، نبش کوچه‌ی مهر، پلاک ۱۲، طبقه‌ی دوم"
    )
    PHONE = os.environ.get("ASA_PHONE", "021-88 12 34 56")
    # تاریخ شمسی و «گذشته بودن ساعت» هر دو با همین منطقه محاسبه می‌شوند.
    TIMEZONE = os.environ.get("TZ_NAME", "Asia/Tehran")

    WORK_START = os.environ.get("ASA_WORK_START", "16:00")
    WORK_END = os.environ.get("ASA_WORK_END", "22:00")        # ساعت بستن مطب
    SLOT_MINUTES = _int("ASA_SLOT_MINUTES", 30)
    CABINS = _int("ASA_CABINS", 10)
    CABIN_CAPACITY = _int("ASA_CABIN_CAPACITY", 1)           # بخش «ظرفیت — محاسبه» را ببینید
    STRIP_DAYS = _int("ASA_STRIP_DAYS", 30)
    FEW_LEFT_THRESHOLD = _int("ASA_FEW_LEFT", 3)

    # --- پیامک ---
    SMS_PROVIDER = os.environ.get("SMS_PROVIDER", "file")
    KAVENEGAR_KEY = os.environ.get("KAVENEGAR_KEY", "")
    KAVENEGAR_LINE = os.environ.get("KAVENEGAR_LINE", "")
    SMSIR_KEY = os.environ.get("SMSIR_KEY", "")
    SMSIR_LINE = os.environ.get("SMSIR_LINE", "")
    SMS_WEBHOOK_URL = os.environ.get("SMS_WEBHOOK_URL", "")
    SMS_INBOUND_SECRET = os.environ.get("SMS_INBOUND_SECRET", "asa-dev-inbound-secret")
    SMS_LOG_PATH = os.environ.get("ASA_SMS_LOG", str(DATA_DIR / "sms_outbox.log"))
    SMS_BUDGET_MONTHLY_USD = _fl("SMS_BUDGET_MONTHLY_USD", 3.0)
    SMS_UNIT_COST_USD = _fl("ASA_SMS_UNIT_COST_USD", 0.006)
    REMINDER_LEAD_MIN = _int("REMINDER_LEAD_MIN", 120)
    REMINDER_INTERVAL_SEC = _int("ASA_REMINDER_INTERVAL_SEC", 60)
    RETURN_GAP_DAYS = _int("ASA_RETURN_GAP_DAYS", 30)

    # --- محدودیت‌ها (لایه ۰۹) ---
    LIMIT_REGISTER_IP = _int("ASA_LIMIT_REGISTER_IP", 25)          # ۲۴ ساعته / IP
    LIMIT_REGISTER_CODE = _int("ASA_LIMIT_REGISTER_CODE", 3)       # ۲۴ ساعته / کد ملی
    LIMIT_LOGIN_IP = _int("ASA_LIMIT_LOGIN_IP", 10)                 # هر ۱۵ دقیقه / IP
    LIMIT_LOGIN_CODE = _int("ASA_LIMIT_LOGIN_CODE", 8)             # هر ۱۵ دقیقه / کد ملی
    LIMIT_LOGIN_FAIL_LOCK = _int("ASA_LIMIT_LOGIN_FAIL_LOCK", 5)   # قفل رکورد پس از N خطا
    LIMIT_RESERVE_DAY = _int("ASA_LIMIT_RESERVE_DAY", 4)           # روزانه / بیمار
    LIMIT_INBOUND_MIN = _int("ASA_LIMIT_INBOUND_MIN", 120)         # دقیقه‌ای / IP

    UPLOAD_MAX_BYTES = _int("ASA_UPLOAD_MAX_BYTES", 5 * 1024 * 1024)

    # نشست بیمار: ۶ ساعت، HttpOnly + SameSite=Strict (تولید). در dev برای
    # راحتیِ پیش‌نمایش SameSite=Lax می‌شود (همان‌سایت است، بدون CSRF مشکل).
    PATIENT_SESSION_HOURS = _int("ASA_PATIENT_SESSION_HOURS", 6)
    ADMIN_SESSION_HOURS = _int("ASA_ADMIN_SESSION_HOURS", 8)
    CSRF_COOKIE_NAME = "asa_csrf"
    COOKIE_SECURE = _b("ASA_COOKIE_SECURE", APP_ENV == "production")
    COOKIE_SAMESITE = os.environ.get("ASA_COOKIE_SAMESITE") or (
        "Strict" if APP_ENV == "production" else "Lax"
    )

    ADMIN_USER = os.environ.get("ASA_ADMIN_USER", "dr-asa")
    ADMIN_PASSWORD = os.environ.get("ASA_ADMIN_PASSWORD", "asa-demo-1385")

    @classmethod
    def tz(cls):
        return ZoneInfo(cls.TIMEZONE)

    @classmethod
    def now(cls):
        return datetime_now(cls.tz())

    @classmethod
    def slot_times(cls) -> list[str]:
        """خانه‌های ساعت: 16:00 تا 21:30 با گام ۳۰ دقیقه (WORK_END بسته می‌شود)."""
        from datetime import datetime, timedelta

        start = datetime.strptime(cls.WORK_START, "%H:%M")
        end = datetime.strptime(cls.WORK_END, "%H:%M")
        step = timedelta(minutes=cls.SLOT_MINUTES)
        out = []
        cur = start
        while cur + step <= end:
            out.append(cur.strftime("%H:%M"))
            cur += step
        return out


def datetime_now(tz):
    from datetime import datetime

    return datetime.now(tz)
