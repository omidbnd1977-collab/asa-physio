"""Layer 03 — connection handling plus a forward-only, versioned migration runner."""

from __future__ import annotations

import hashlib
import pathlib
import re
import sqlite3
import threading

from .config import ROOT, settings
from .logging_ import info, warn

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"
_local = threading.local()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_conn() -> sqlite3.Connection:
    """One connection per thread; SQLite objects are not shareable across threads."""
    c = getattr(_local, "conn", None)
    if c is None:
        c = connect()
        _local.conn = c
    return c


def close_conn() -> None:
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


# --------------------------------------------------------------------------
# migrations
# --------------------------------------------------------------------------
BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""


def _files() -> list[pathlib.Path]:
    return sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons.

    `executescript()` cannot be used: it issues an implicit COMMIT, which would break the
    atomicity of a migration. Trigger bodies (BEGIN ... END) are rejected outright — layer
    03 forbids them, so the simple splitter is always sufficient.
    """
    if re.search(r"\bCREATE\s+(TEMP\s+)?TRIGGER\b", sql, re.I):
        raise RuntimeError("triggers are not allowed: business logic belongs in api/repo.py")
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    quote: str | None = None
    while i < n:
        ch = sql[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    buf.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i)
            i = n if j == -1 else j + 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def migrate(conn: sqlite3.Connection | None = None) -> list[str]:
    """Apply every pending migration in order. Refuses to run if an applied file changed."""
    conn = conn or get_conn()
    conn.executescript(BOOTSTRAP)
    applied = {r["version"]: r["checksum"] for r in conn.execute("SELECT * FROM schema_migrations")}
    done: list[str] = []
    for path in _files():
        version = path.name[:4]
        body = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(body.encode()).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(
                    f"migration {path.name} changed after it was applied — "
                    "migrations are forward only, add a new file instead"
                )
            continue
        statements = split_statements(body)
        conn.execute("BEGIN")
        try:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (?,?)",
                (version, checksum),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        done.append(path.name)
        info("migration.applied", migration=path.name)
    if not done:
        info("migration.up_to_date", count=len(applied))
    return done


def assert_no_business_triggers(conn: sqlite3.Connection | None = None) -> list[str]:
    """Layer 03 guard — the schema must contain no triggers at all."""
    conn = conn or get_conn()
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    names = [r["name"] for r in rows]
    if names:
        warn("schema.trigger_found", triggers=names)
    return names


def tables(conn: sqlite3.Connection | None = None) -> list[str]:
    conn = conn or get_conn()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


if __name__ == "__main__":  # python -m api.db
    from .logging_ import setup_logging

    setup_logging()
    applied = migrate()
    print(f"db: {settings.DB_PATH}")
    print("applied:", applied or "nothing (already up to date)")
    print("tables:", ", ".join(tables()))
    print("triggers:", assert_no_business_triggers() or "none")
    _ = ROOT
