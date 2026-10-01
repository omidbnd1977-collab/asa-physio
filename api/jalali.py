"""تاریخ شمسی — الگوریتم FarsiWeb/jalali-core (همان که jdatetime و jalaali_core
استفاده می‌کنند) + قالب‌های فارسی. بدون وابستگی خارجی؛ صحتش در `scripts/check.py`
روی ۵۰ سال روز‌به‌روز با `jalali_core` سنجیده می‌شود.
"""
from __future__ import annotations

from datetime import date, timedelta

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
G_MONTH_LENGTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
J_MONTH_LENGTH = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
MONTH_NAMES = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]
WEEKDAYS = [  # index 0 = شنبه، چون date.weekday() برای شنبه صفر است
    "شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه",
]
WEEKDAYS_SHORT = ["ش", "ی", "د", "س", "چ", "پ", "ج"]


def to_persian_digits(value) -> str:
    out = str(value)
    return "".join(PERSIAN_DIGITS[int(c)] if c.isdigit() else c for c in out)


def _is_g_leap(gy: int) -> bool:
    return (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0


def to_jalali(d: date) -> tuple[int, int, int]:
    """میلادی → شمسی."""
    gy = d.year - 1600
    gm = d.month - 1
    day_no = (365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
              + d.day - 1 - 79)
    day_no += sum(G_MONTH_LENGTH[i] for i in range(gm))
    if gm > 1 and _is_g_leap(d.year):
        day_no += 1

    j_np = day_no // 12053
    day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (day_no // 1461)
    day_no %= 1461
    if day_no >= 366:
        day_no -= 1
        jy += day_no // 365
        day_no %= 365
    i = 0
    for i in range(11):
        if day_no < J_MONTH_LENGTH[i]:
            i -= 1
            break
        day_no -= J_MONTH_LENGTH[i]
    return jy, i + 2, day_no + 1


def from_jalali(jy: int, jm: int, jd: int) -> date:
    """شمسی → میلادی."""
    y = jy - 979
    day_no = 365 * y + (y // 33) * 8 + (y % 33 + 3) // 4 + jd - 1 + 79
    day_no += sum(J_MONTH_LENGTH[i] for i in range(jm - 1))

    gy = 1600 + 400 * (day_no // 146097)
    day_no %= 146097
    leap = 1
    if day_no >= 36525:
        day_no -= 1
        gy += 100 * (day_no // 36524)
        day_no %= 36524
        if day_no >= 365:
            day_no += 1
        else:
            leap = 0
    gy += 4 * (day_no // 1461)
    day_no %= 1461
    if day_no >= 366:
        leap = 0
        day_no -= 1
        gy += day_no // 365
        day_no %= 365
    month = 0
    lengths = list(G_MONTH_LENGTH)
    if leap:
        lengths[1] = 29
    while day_no >= lengths[month]:
        day_no -= lengths[month]
        month += 1
    return date(gy, month + 1, day_no + 1)


def jday_number(jy: int, jm: int, jd: int) -> int:
    """عدد ترتیبیِ روزِ شمسی (برای اختلاف روز)."""
    return from_jalali(jy, jm, jd).toordinal()


def j_days_between(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return jday_number(*b) - jday_number(*a)


def _jalali_leap_count(year: int) -> int:
    """چند کبیسه تا «پایان سال year-ی» رخ داده (فرمول ارجاع jalaali-core)."""
    if year >= 0:
        return (year - 1) // 33 * 8 + ((year - 1) % 33 + 3) // 4
    return -((-year + 4) // 33 * 8 + ((32 - year) % 33 - 3) // 4)


def is_j_leap(jy: int) -> bool:
    return _jalali_leap_count(jy + 1) - _jalali_leap_count(jy) == 1


def j_month_length(jy: int, jm: int) -> int:
    return J_MONTH_LENGTH[jm - 1] + (1 if (jm == 12 and is_j_leap(jy)) else 0)


def jalali_of(d: date) -> dict:
    jy, jm, jd = to_jalali(d)
    return {
        "jy": jy, "jm": jm, "jd": jd,
        "weekday": d.weekday(),
        "weekday_name": WEEKDAYS[d.weekday()],
        "weekday_short": WEEKDAYS_SHORT[d.weekday()],
        "month_name": MONTH_NAMES[jm - 1],
        "iso": f"{jy:04d}-{jm:02d}-{jd:02d}",
        "slash": to_persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d}"),
        "long": (f"{WEEKDAYS[d.weekday()]} {to_persian_digits(jd)} "
                 f"{MONTH_NAMES[jm - 1]} {to_persian_digits(jy)}"),
        "medium": f"{to_persian_digits(jd)} {MONTH_NAMES[jm - 1]}",
        "gregorian": d.isoformat(),
    }


def format_jdatetime(dt) -> str:
    j = jalali_of(dt.date())
    return f"{j['long']}  {to_persian_digits(dt.strftime('%H:%M'))}"


def date_range(start: date, days: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(days)]
