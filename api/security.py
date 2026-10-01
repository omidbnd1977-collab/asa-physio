"""اعتبارسنجی ورودی، رمزنگاریِ بی‌خطرِ لاگ، CSRF، و سقف‌های لایه‌ی ۰۹."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time

from .config import settings

# ---------------------------------------------------------------- ارقام فارسی
_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGITS_TRANS = str.maketrans(_FA_DIGITS + _AR_DIGITS, "0123456789" * 2)


def latinize_digits(value: str) -> str:
    """ارقام فارسی/عربی به لاتین. بقیه‌ی نویسه‌ها دست‌نخورده می‌مانند
    (جداکننده‌ها را هر مصرف‌کننده خودش با regex می‌زند)."""
    return (value or "").translate(_DIGITS_TRANS)



# ------------------------------------------------------------------ کد ملی
def validate_national_code(raw: str) -> tuple[bool, str, str | None]:
    """الگوریتم رسمی رقم کنترل. برمی‌گرداند (ok, پیام_خطا, کد_نرمال).

    سه رقم اولِ پرکاربرد مثل 0012345678 در دیتابیس قدیمی به شکل 12345678 ذخیره
    می‌شدند؛ اینجا همیشه ۱۰ رقمی با پیش‌شماره می‌کنیم تا تطبیق یکتا بماند.
    """
    code = re.sub(r"\D", "", latinize_digits(raw or ""))
    if len(code) < 8 or len(code) > 10:
        return False, "کد ملی باید ۱۰ رقم باشد.", None
    code = code.zfill(10)
    if code == "0" * 10:
        return False, "کد ملی معتبر نیست.", None
    if len(set(code)) == 1:
        return False, "کد ملی معتبر نیست.", None
    total = sum(int(code[i]) * (10 - i) for i in range(9))
    check = int(code[9])
    remainder = total % 11
    ok = (check == remainder) if remainder < 2 else (check == 11 - remainder)
    if not ok:
        return False, "رقمِ کنترل کد ملی می‌خواند؛ یک رقم را بررسی کنید.", None
    return True, "", code


def national_code_fingerprint(code: str) -> str:
    return fingerprint(f"nc:{code}")


def fingerprint(value: str) -> str:
    """برای لاگ: «09121112233» → «fp_5c2a19…» — بدون salt قابل برگشت نیست
    ولی با کلیدِ نشست‌به‌نشست ثابت می‌ماند تا ردگیریِ تکراری‌ها ممکن باشد."""
    key = settings.SECRET_KEY.encode()
    digest = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
    return "fp_" + digest[:12]


# ------------------------------------------------------------------ موبایل
def normalize_mobile(raw: str) -> tuple[bool, str, str | None]:
    """`+98`، `0098`، `09…`، ارقام فارسی، فاصله و پرانتز همه یکی می‌شوند: 0098XXXXXXXXXX."""
    digits = re.sub(r"\D", "", latinize_digits(raw or ""))
    if digits.startswith("0098") and len(digits) == 14:
        digits = digits[4:]
    elif digits.startswith("98") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) != 10:
        return False, "شماره باید ۱۰ رقم بعد از پیش‌شماره باشد (مثال 09121112233).", None
    if digits[0] != "9":
        return False, "شماره‌ی همراه با ۰۹ شروع می‌شود.", None
    if digits[1] not in "01234456789":
        return False, "شماره‌ی همراه معتبر نیست.", None
    return True, "", "0098" + digits


def mobile_display(canonical: str) -> str:
    """00989121112233 → ۰۹۱۲ ۱۱۱ ۲۲۳۳ (نمایش)."""
    d = re.sub(r"\D", "", canonical or "")
    if d.startswith("0098"):
        d = d[2:]
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10:
        return latinize_to_fa(f"{d[:4]} {d[4:7]} {d[7:]}")
    return latinize_to_fa(canonical or "")


def latinize_to_fa(value: str) -> str:
    return "".join(_FA_DIGITS[int(c)] if c.isdigit() else c for c in (value or ""))


# ------------------------------------------------------------------- تاریخ
_DATE_SPLIT = re.compile(r"^(\d{4})[-/.]?\s*(\d{1,2})[-/.]?\s*(\d{1,2})$")


def parse_jalali_date(y: str, m: str, d: str) -> tuple[bool, str, tuple[int, int, int] | None]:
    """ورودی ماسک‌شده؛ ارقام فارسی هم پذیرفته می‌شود. تاریخ باید واقعی باشد."""
    from .jalali import from_jalali, j_month_length, jalali_of

    digits = [re.sub(r"\D", "", latinize_digits(str(v or ""))) for v in (y, m, d)]
    if any(not x for x in digits):
        return False, "سال، ماه و روز را کامل وارد کنید.", None
    jy, jm, jd = int(digits[0]), int(digits[1]), int(digits[2])
    if not (1300 <= jy <= 1500):
        return False, "سال تولد منطقی نیست.", None
    if not (1 <= jm <= 12):
        return False, "ماه باید بین ۱ تا ۱۲ باشد.", None
    if not (1 <= jd <= j_month_length(jy, jm)):
        return False, "چنین روزی در این ماه وجود ندارد.", None
    greg = from_jalali(jy, jm, jd)
    today = jalali_of(settings.now().date())
    if (jy, jm, jd) > (today["jy"], today["jm"], today["jd"]):
        return False, "تاریح تولد نمی‌تواند در آینده باشد.".replace("تاریح", "تاریخ"), None
    return True, "", (jy, jm, jd)


# --------------------------------------------------------------- لینک / فایل
_HTTP = re.compile(r"^https?://[^\s]+$", re.I)


def validate_url(raw: str) -> tuple[bool, str]:
    raw = (raw or "").strip()
    if not raw:
        return True, ""
    if not _HTTP.match(raw) or " " in raw:
        return False, "لینک باید با http:// یا https:// شروع شود."
    return True, ""


MAGIC_SIGNATURES = [
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"%PDF-", "application/pdf", ".pdf"),
]


def sniff_magic(head: bytes) -> tuple[str, str, str] | None:
    """نوع فایل با magic bytes، نه با پسوند. WebP: RIFF....WEBP."""
    for sig, mime, ext in MAGIC_SIGNATURES:
        if head.startswith(sig):
            return mime, ext, sig.decode("latin-1", "replace")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp", "RIFF"
    return None


def upload_extension(head: bytes) -> tuple[bool, str, str, str | None]:
    """(ok, پیام, mime, ext). یک .png که اجراشدنی است رد می‌شود."""
    sniffed = sniff_magic(head)
    if not sniffed:
        return False, "فرمت فایل شناخته نشد؛ فقط JPG/PNG/WEBP/PDF.", "", None
    mime, ext, _ = sniffed
    return True, "", mime, ext


def content_filename(blob: bytes, ext: str, prefix: str = "file") -> str:
    """نام فایل از محتوا ساخته می‌شود، نه از نام ارسالی کاربر."""
    digest = hashlib.sha256(blob).hexdigest()[:32]
    return f"{prefix}-{digest}{ext}"


def upload_path(base_dir: str | os.PathLike, name: str) -> tuple[bool, str, str | None]:
    """هر مسیری که از پوشه بیرون بزند رد می‌شود."""
    base = os.path.realpath(str(base_dir))
    target = os.path.realpath(os.path.join(base, os.path.basename(name or "")))
    if not target.startswith(base + os.sep):
        return False, "مسیر نامعتبر.", None
    return True, "", target


def save_blob(blob: bytes, prefix: str) -> tuple[bool, str, str | None, str | None]:
    """(ok, پیام_خطا, نام_blob, mime). نام از محتوا ساخته می‌شود، نه از نام کاربر."""
    if not blob:
        return False, "فایل خالی است.", None, None
    ok, msg, mime, ext = upload_extension(blob[:16])
    if not ok:
        return False, msg, None, None
    if len(blob) > settings.UPLOAD_MAX_BYTES:
        return False, "حجم فایل بیشتر از ۵ مگابایت است.", None, None
    name = content_filename(blob, ext, prefix)
    ok, msg, full = upload_path(settings.UPLOAD_DIR, name)
    if not ok:
        return False, msg, None, None
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(blob)
    return True, "", name, mime


# ------------------------------------------------------------------- نشست
def issue_csrf() -> str:
    token = secrets.token_urlsafe(32)
    return token


def check_csrf(request, expected: str | None = None) -> tuple[bool, str]:
    """کوکی + هدر/فیلد باید با هم بخوانند.

    اگر `expected` (توکنِ نشست) داده شود، همان هم مقایسه می‌شود؛ این‌طور یک
    توکنِ کوکیِ دزدیِ زیردامنه‌ای به‌تنهایی کافی نیست.
    """
    cookie = request.cookies.get(settings.CSRF_COOKIE_NAME, "")
    sent = request.headers.get("X-CSRF-Token") or (request.form.get("csrf_token") or "")
    if not sent and getattr(request, "is_json", False):
        sent = (request.get_json(silent=True) or {}).get("csrf_token") or ""
    if not cookie or not sent:
        return False, "کوکی CSRF نیست؛ صفحه را تازه کنید و دوباره تلاش کنید."
    if not hmac.compare_digest(cookie, sent):
        return False, "توکن CSRF نمی‌خواند."
    if expected is not None and not hmac.compare_digest(expected, sent):
        return False, "توکن CSRF به این نشست تعلق ندارد."
    return True, ""


def cookie_kwargs() -> dict:
    return dict(
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.PATIENT_SESSION_HOURS * 3600,
    )


def auth_hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _sign(token: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def make_admin_token() -> str:
    """salt.scrypt(password) — مقدار تصادفیِ هر ورود، پس بازپخش‌ناپذیر."""
    salt = os.urandom(16)
    return salt.hex() + "." + auth_hash(settings.ADMIN_PASSWORD, salt).hex()


def sign(token: str) -> str:
    """امضایی که در state می‌نشیند؛ مقایسه با آن زمان‌ثابت است."""
    return _sign(token or "")


def verify_admin_token(token: str, signature: str) -> bool:
    """two-time-constant compare: اول امضا، بعد hash رمز.

    `token` از state سرور می‌آید (نه ورودی کاربر)، پس timing روی آن ریسک ندارد؛
    باز هم هر دو مقایسه `compare_digest` هستند تا اگر روزی توکن از کلاینت
    بیاید، این تابع آماده باشد.
    """
    if not token or not signature:
        return False
    if not hmac.compare_digest(_sign(token), signature):
        return False
    try:
        salt_hex, digest_hex = token.split(".", 1)
        supplied = bytes.fromhex(digest_hex)
        expected = auth_hash(settings.ADMIN_PASSWORD, bytes.fromhex(salt_hex))
    except (ValueError, IndexError):
        return False
    return hmac.compare_digest(supplied, expected)


# --------------------------------------------------------- سقف‌ها (لایه ۰۹)
class RateLimiter:
    """شمارنده‌ی کشیِ ۲۴ ساعته/۱۵ دقیقه‌ای. برای نمونه‌ی تک‌پروسی; در پروداکشن
    به Redis منتقل می‌شود (کلیدها همین شکل‌اند)."""

    def __init__(self):
        self._hits: dict[tuple[str, str], list[float]] = {}

    def _window(self, bucket: str) -> int:
        return {
            "register_ip": 86400, "register_code": 86400,
            "login_ip": 900, "login_code": 900,
            "login_fail": 3600,
            "reserve_day": 86400, "inbound_min": 60,
        }.get(bucket, 60)

    def hit(self, bucket: str, key: str, cost: int = 1) -> tuple[bool, int, int]:
        window = self._window(bucket)
        now = time.time()
        cell = self._hits.setdefault((bucket, key), [])
        keep = [t for t in cell if now - t < window]
        if len(keep) >= self.limit_for(bucket):
            self._hits[(bucket, key)] = keep
            return False, int(keep[0] + window - now), len(keep)
        keep.extend([now] * cost)
        self._hits[(bucket, key)] = keep
        return True, window, len(keep)

    def limit_for(self, bucket: str) -> int:
        return {
            "register_ip": settings.LIMIT_REGISTER_IP,
            "register_code": settings.LIMIT_REGISTER_CODE,
            "login_ip": settings.LIMIT_LOGIN_IP,
            "login_code": settings.LIMIT_LOGIN_CODE,
            "login_fail": settings.LIMIT_LOGIN_FAIL_LOCK,
            "reserve_day": settings.LIMIT_RESERVE_DAY,
            "inbound_min": settings.LIMIT_INBOUND_MIN,
        }.get(bucket, 60)

    def snapshot(self) -> list[dict]:
        now = time.time()
        out = []
        for (bucket, key), cell in sorted(self._hits.items()):
            live = [t for t in cell if now - t < self._window(bucket)]
            if live:
                out.append({
                    "bucket": bucket, "key": key, "used": len(live),
                    "limit": self.limit_for(bucket),
                    "reset_in": int(live[0] + self._window(bucket) - now),
                })
        return out


limiter = RateLimiter()

BUCKET_LABELS = {
    "register_ip": "ثبت‌نام / IP",
    "register_code": "ثبت‌نام / کد ملی",
    "login_ip": "ورود / IP",
    "login_code": "ورود / کد ملی",
    "login_fail": "قفل پس از خطا",
    "reserve_day": "رزرو / بیمار",
    "inbound_min": "وبهوک ورودی / IP",
}
