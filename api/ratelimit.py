"""Layer 09 — per-IP, per-identity and global limits plus a circuit breaker.

Counters live in SQLite so they survive a restart and are shared by every worker.
`docs/SCALE.md` names this as the measured bottleneck and Redis as the 10x step.
"""

from __future__ import annotations

import time

from . import repo
from .config import settings
from .errors import CircuitOpen, RateLimited
from .logging_ import fingerprint, warn
from .policies import SYSTEM

_last_gc = 0.0


def _gc(now: float) -> None:
    global _last_gc
    if now - _last_gc < 60:
        return
    _last_gc = now
    repo.delete(SYSTEM, "rate_events", where="at < ?", params=[now - 86400])
    repo.delete(
        SYSTEM,
        "idempotency_keys",
        where="created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')",
    )


def hit(bucket: str, limit: int, window_s: int, *, cost: int = 1) -> tuple[bool, int]:
    """Sliding window. Returns (allowed, retry_after_seconds)."""
    now = time.time()
    _gc(now)
    cutoff = now - window_s
    with repo.tx() as conn:
        conn.execute("DELETE FROM rate_events WHERE bucket = ? AND at < ?", (bucket, cutoff))
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM rate_events WHERE bucket = ? AND at >= ?", (bucket, cutoff)
        ).fetchone()["n"]
        if used + cost > limit:
            oldest = conn.execute(
                "SELECT MIN(at) AS a FROM rate_events WHERE bucket = ? AND at >= ?",
                (bucket, cutoff),
            ).fetchone()["a"]
            retry = max(1, int((oldest + window_s) - now) + 1) if oldest else window_s
            return False, retry
        conn.executemany("INSERT INTO rate_events(bucket, at) VALUES (?,?)", [(bucket, now)] * cost)
    return True, 0


def enforce(
    bucket: str, limit: int, window_s: int, *, message: str | None = None, cost: int = 1
) -> None:
    ok, retry = hit(bucket, limit, window_s, cost=cost)
    if not ok:
        warn("ratelimit.blocked", bucket=bucket, limit=limit, window_s=window_s)
        raise RateLimited(message or RateLimited.message, retry_after=retry)


def ip_bucket(prefix: str, ip: str) -> str:
    return f"{prefix}:ip:{fingerprint(ip)}"


def id_bucket(prefix: str, identity: str) -> str:
    return f"{prefix}:id:{fingerprint(identity)}"


# --------------------------------------------------------------------------
# global circuit breaker
# --------------------------------------------------------------------------
BREAKER = "global"


def breaker_state() -> tuple[bool, float, str]:
    rows = repo.select(SYSTEM, "breaker_state", where="name = ?", params=[BREAKER], limit=1)
    if not rows:
        return False, 0.0, ""
    until = float(rows[0]["open_until"])
    return (until > time.time()), until, rows[0]["reason"]


def breaker_check() -> None:
    is_open, until, reason = breaker_state()
    if is_open:
        raise CircuitOpen(
            retry_after=max(1, int(until - time.time()) + 1), detail=f"breaker open: {reason}"
        )


def breaker_open(reason: str, seconds: int | None = None) -> None:
    secs = seconds or settings.BREAKER_OPEN_S
    until = time.time() + secs
    with repo.tx() as conn:
        conn.execute(
            "INSERT INTO breaker_state(name, open_until, reason) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET open_until = excluded.open_until, "
            "reason = excluded.reason",
            (BREAKER, until, reason[:200]),
        )
    warn("breaker.opened", reason=reason[:200], seconds=secs)


def breaker_close() -> None:
    with repo.tx() as conn:
        conn.execute(
            "INSERT INTO breaker_state(name, open_until, reason) VALUES (?,0,'') "
            "ON CONFLICT(name) DO UPDATE SET open_until = 0, reason = ''",
            (BREAKER,),
        )


def record_server_error() -> None:
    """Too many 5xx in a short window trips the breaker and sheds load."""
    ok, _ = hit("breaker:errors", settings.BREAKER_ERR_THRESHOLD, settings.BREAKER_WINDOW_S)
    if not ok:
        breaker_open(
            f">{settings.BREAKER_ERR_THRESHOLD} server errors in {settings.BREAKER_WINDOW_S}s"
        )
