"""پایگاه‌داده: schema، WAL، و کمکی‌های تراکنش.

تخصیص کابین داخل `BEGIN IMMEDIATE` انجام می‌شود و
`UNIQUE (slot_date, slot_time, cabin)` ضمانت نهایی است — همان‌طور که
BOOKING.md خواسته. برای اینکه همان تراکنش «دو نوبتِ یک بیمار در یک ساعت» را
هم جلو بگیرد، `UNIQUE (patient_id, slot_date, slot_time)` هم اضافه شده است.
"""
from __future__ import annotations

import os
import sqlite3
import threading

from .config import settings

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name     TEXT    NOT NULL,
  national_code TEXT    NOT NULL UNIQUE,          -- ۱۰ رقم نرمال‌شده
  birth_jdate   TEXT    NOT NULL,                 -- YYYY-MM-DD شمسی
  mobile        TEXT    NOT NULL,                 -- 0098…
  mri_url       TEXT,
  orthopedist   TEXT,
  meds_photo    TEXT,                             -- نام blob، نه مسیر کاربر
  meds_mime     TEXT,
  mri_file      TEXT,                             -- فایل MRI (نام blob)
  mri_mime      TEXT,
  note          TEXT,                             -- یادداشت پزشک
  created_at    TEXT    NOT NULL,
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS appointments (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id       INTEGER NOT NULL REFERENCES patients(id),
  slot_date        TEXT    NOT NULL,              -- YYYY-MM-DD شمسی
  slot_time        TEXT    NOT NULL,              -- HH:MM
  cabin            INTEGER NOT NULL,
  kind           TEXT    NOT NULL DEFAULT 'initial',  -- initial|reschedule|followup
  status           TEXT    NOT NULL DEFAULT 'booked', -- booked|coming|done|cancelled|no_show
  tracking_code    TEXT    NOT NULL UNIQUE,
  idempotency_key  TEXT,
  note             TEXT,
  orthopedist      TEXT,
  mri_url          TEXT,
  cabin_prev       INTEGER,
  slot_prev_date   TEXT,
  slot_prev_time   TEXT,
  confirm_sent_at  TEXT,
  confirm_reply_at TEXT,
  reminder_attempts INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT    NOT NULL,
  updated_at       TEXT    NOT NULL,
  UNIQUE (slot_date, slot_time, cabin),
  UNIQUE (patient_id, slot_date, slot_time),
  UNIQUE (patient_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, slot_date, slot_time);
CREATE INDEX IF NOT EXISTS idx_appt_remind  ON appointments(status, confirm_sent_at, slot_date, slot_time);

CREATE TABLE IF NOT EXISTS sms_messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id    INTEGER,
  appointment_id INTEGER,
  direction     TEXT    NOT NULL DEFAULT 'out',
  event         TEXT    NOT NULL,
  to_phone      TEXT    NOT NULL,
  body          TEXT    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'pending',   -- sent|failed|skipped_budget
  error         TEXT,
  provider_id   TEXT,
  cost_usd      REAL    NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL,
  sent_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_sms_created ON sms_messages(created_at DESC);

CREATE TABLE IF NOT EXISTS sms_inbound (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  phone      TEXT    NOT NULL,
  body       TEXT    NOT NULL,
  normalized TEXT,
  matched_id INTEGER,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS body_parts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  label      TEXT    NOT NULL UNIQUE,
  grp        TEXT    NOT NULL DEFAULT 'سایر',
  source     TEXT    NOT NULL DEFAULT 'seed',        -- seed|physician
  used_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS treatments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  label      TEXT    NOT NULL UNIQUE,
  source     TEXT    NOT NULL DEFAULT 'seed',
  used_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS patient_sessions (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  patient_id     INTEGER NOT NULL REFERENCES patients(id),
  appointment_id INTEGER REFERENCES appointments(id),
  session_date   TEXT    NOT NULL,                   -- YYYY-MM-DD شمسی
  pain_before    INTEGER,
  pain_after     INTEGER,
  findings       TEXT,
  next_plan      TEXT,
  created_by     TEXT,
  created_at     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sess_patient ON patient_sessions(patient_id, session_date DESC);

CREATE TABLE IF NOT EXISTS session_body_parts (
  session_id INTEGER NOT NULL REFERENCES patient_sessions(id) ON DELETE CASCADE,
  part_id    INTEGER NOT NULL REFERENCES body_parts(id),
  PRIMARY KEY (session_id, part_id)
);

CREATE TABLE IF NOT EXISTS session_treatments (
  session_id   INTEGER NOT NULL REFERENCES patient_sessions(id) ON DELETE CASCADE,
  treatment_id INTEGER NOT NULL REFERENCES treatments(id),
  PRIMARY KEY (session_id, treatment_id)
);

CREATE TABLE IF NOT EXISTS site_requests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT,
  phone      TEXT,
  topic      TEXT,
  message    TEXT,
  source     TEXT,
  handled    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT    NOT NULL,
  amount_usd REAL    NOT NULL DEFAULT 0,
  ref        TEXT,
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_month ON cost_events(created_at);

CREATE TABLE IF NOT EXISTS counters (
  name  TEXT PRIMARY KEY,
  value INTEGER NOT NULL DEFAULT 0
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(str(settings.DATA_DIR), exist_ok=True)
    os.makedirs(str(settings.UPLOAD_DIR), exist_ok=True)
    conn = sqlite3.connect(str(settings.DB_PATH), timeout=15, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


_local = threading.local()


def db() -> sqlite3.Connection:
    """اتصال به‌ازای هر رشته.

    چرا؟ چون `BEGIN IMMEDIATE` روی یک *رشته* معنا دارد؛ اگر همه‌ی رشته‌ها یک
    اتصال را шарینگ کنند، تراکنشِ یک نفر داخل تراکنشِ نفر دیگر باز می‌شود.
    با اتصال جدا + WAL، SQLite خودِ `BEGIN IMMEDIATE` را صف می‌کند
    (busy_timeout ۱۵ ثانیه) و دقیقاً همان «قفلِ سراسری» به‌دست می‌آید که
    BOOKING.md برای تخصیص کابین می‌خواهد.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        with _lock:
            conn = connect()
        _local.conn = conn
    return conn


def reset_for_tests() -> None:
    """اتصالِ رشته‌ی جاری را می‌بندد (فقط در تست)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def init() -> None:
    conn = db()
    conn.executescript(SCHEMA)


def tx(immediate: bool = False):
    """context manager: BEGIN [IMMEDIATE] … COMMIT / ROLLBACK."""
    return _Tx(db(), immediate)


class _Tx:
    def __init__(self, conn: sqlite3.Connection, immediate: bool):
        self.conn = conn
        self.immediate = immediate

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False


def now_iso() -> str:
    return settings.now().isoformat(timespec="seconds")


def bump(conn: sqlite3.Connection, name: str, amount: int = 1) -> int:
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
        (name, amount),
    )
    row = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()
    return int(row["value"]) if row else 0


def month_cost_usd(conn: sqlite3.Connection) -> float:
    start = settings.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM cost_events "
        "WHERE kind = 'sms' AND created_at >= ?",
        (start.isoformat(timespec="seconds"),),
    ).fetchone()
    return float(row["total"] or 0.0)
