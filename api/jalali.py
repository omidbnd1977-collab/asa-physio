"""Jalali (Shamsi) calendar conversion and Iranian national-id validation.

Pure Python, no dependency. Dates are stored in the database as Gregorian ISO
(`YYYY-MM-DD`) so SQL comparisons and sorting stay correct; Jalali is a display and
input format only.
"""

from __future__ import annotations

import datetime as dt
import re

MONTHS_FA = [
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
]
WEEKDAYS_FA = ["شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"]

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Tehran is UTC+03:30 all year (Iran dropped DST in 2022).
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))

_G_DAYS = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
_J_DAYS = [0, 31, 62, 93, 124, 155, 186, 216, 246, 276, 306, 336]


def ascii_digits(value: str) -> str:
    return str(value).translate(_DIGITS)


def _div(a: int, b: int) -> int:
    return a // b


def g2j(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Gregorian -> Jalali.

    BUGFIX (1405): the day-of-cycle branch used to read
        jy += _div(j_day_no - 366, 365)
    which is one year short every time the cycle passed day 366 — that is how
    2026-10-01 came out as 1404/07/09 instead of 1405/07/09 (`j2g` was always
    right, so the round trip g2j(j2g(y, m, d)) silently failed, e.g. for 1403).
    The reference algorithm (FarsiWeb `jalali-core`, what jdatetime uses) takes
    one off *before* the division:
        j_day_no -= 1 ;  jy += _div(j_day_no, 365) ;  j_day_no %= 365
    Both lines below keep the original structure; only this subtraction moved.
    Verified day-by-day against jdatetime for 1900..2100 (73,049 days, 0 diffs).
    """
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + _div(gy2 + 3, 4) - _div(gy2 + 99, 100) + _div(gy2 + 399, 400)
    g_day_no += _G_DAYS[gm2] + gd2
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    j_day_no = g_day_no - 79
    j_np = _div(j_day_no, 12053)
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * _div(j_day_no, 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        j_day_no -= 1
        jy += _div(j_day_no, 365)
        j_day_no %= 365
    i = 0
    while i < 11 and j_day_no >= (31 if i < 6 else 30):
        j_day_no -= 31 if i < 6 else 30
        i += 1
    return jy, i + 1, j_day_no + 1


def j2g(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Jalali -> Gregorian."""
    jy2 = jy - 979
    jm2 = jm - 1
    jd2 = jd - 1
    j_day_no = 365 * jy2 + _div(jy2, 33) * 8 + _div((jy2 % 33) + 3, 4)
    j_day_no += _J_DAYS[jm2] + jd2
    g_day_no = j_day_no + 79
    gy = 1600 + 400 * _div(g_day_no, 146097)
    g_day_no %= 146097
    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * _div(g_day_no, 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False
    gy += 4 * _div(g_day_no, 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += _div(g_day_no, 365)
        g_day_no %= 365
    gd = g_day_no + 1
    months = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    for m in range(1, 13):
        if gd <= months[m]:
            gm = m
            break
        gd -= months[m]
    return gy, gm, gd


def is_jalali_leap(jy: int) -> bool:
    return ((jy + 12) % 33) % 4 == 1


def jalali_month_days(jy: int, jm: int) -> int:
    if jm <= 6:
        return 31
    if jm <= 11:
        return 30
    return 30 if is_jalali_leap(jy) else 29


def valid_jalali(jy: int, jm: int, jd: int) -> bool:
    if not (1200 <= jy <= 1600) or not (1 <= jm <= 12):
        return False
    return 1 <= jd <= jalali_month_days(jy, jm)


def parse_jalali(value: str) -> dt.date:
    """Accept 1370/5/3, ۱۳۷۰-۰۵-۰۳, 1370.5.3 -> Gregorian `date`."""
    raw = ascii_digits(value).strip()
    parts = re.split(r"[/\-.\s]+", raw)
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError("تاریخ باید به شکل روز/ماه/سال شمسی باشد، مثل ۱۳۷۰/۰۵/۰۳")
    jy, jm, jd = (int(p) for p in parts)
    if jy < 100:  # 70/5/3 -> 1370/5/3
        jy += 1300
    if not valid_jalali(jy, jm, jd):
        raise ValueError("تاریخ تولد معتبر نیست.")
    gy, gm, gd = j2g(jy, jm, jd)
    return dt.date(gy, gm, gd)


def to_jalali_str(d: dt.date | str) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    jy, jm, jd = g2j(d.year, d.month, d.day)
    return f"{jy:04d}/{jm:02d}/{jd:02d}"


def to_jalali_long(d: dt.date | str) -> str:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d[:10])
    jy, jm, jd = g2j(d.year, d.month, d.day)
    # Python weekday(): Monday=0 .. Sunday=6 ; Jalali week starts on Saturday
    wd = WEEKDAYS_FA[(d.weekday() + 2) % 7]
    return f"{wd} {jd} {MONTHS_FA[jm - 1]} {jy}"


def today_tehran() -> dt.date:
    return dt.datetime.now(TEHRAN).date()


def now_tehran() -> dt.datetime:
    return dt.datetime.now(TEHRAN)


# --------------------------------------------------------------------------
# Iranian national id
# --------------------------------------------------------------------------
def normalize_national_id(value: str) -> str:
    raw = re.sub(r"\D", "", ascii_digits(value))
    return raw.zfill(10) if 8 <= len(raw) < 10 else raw


def valid_national_id(value: str) -> bool:
    """Official check-digit algorithm."""
    nid = normalize_national_id(value)
    if len(nid) != 10 or not nid.isdigit():
        return False
    if nid == nid[0] * 10:  # 0000000000, 1111111111, ... are structurally invalid
        return False
    total = sum(int(nid[i]) * (10 - i) for i in range(9))
    rem = total % 11
    check = int(nid[9])
    return check == rem if rem < 2 else check == 11 - rem
