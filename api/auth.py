"""Layer 04 + 08 — password hashing, sessions, and actor resolution."""

from __future__ import annotations

import datetime as dt
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from . import repo
from .config import settings
from .errors import Forbidden, Unauthorized
from .logging_ import fingerprint, info, warn
from .policies import SYSTEM, Actor

ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
SESSION_COOKIE = "asa_session"
# A real hash of a value nobody knows, so a login attempt for a non-existent user costs
# the same CPU as a real one and account existence cannot be timed.
_DUMMY_HASH = ph.hash(secrets.token_urlsafe(32))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_password(raw: str) -> str:
    if len(raw) < 12:
        raise ValueError("password must be at least 12 characters")
    return ph.hash(raw)


def create_user(username: str, password: str, role: str = "staff") -> int:
    uid = repo.insert(
        SYSTEM,
        "admin_users",
        {
            "username": username.strip().lower(),
            "password_hash": hash_password(password),
            "role": role,
        },
    )
    info("auth.user_created", user_id=uid, role=role)
    return uid


def verify_login(username: str, password: str) -> Actor:
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="username = ? AND is_active = 1",
        params=[username.strip().lower()],
        limit=1,
    )
    # constant-ish work whether or not the user exists, so timing does not leak accounts
    stored = rows[0]["password_hash"] if rows else _DUMMY_HASH
    try:
        ph.verify(stored, password)
        ok = bool(rows)
    except (VerifyMismatchError, InvalidHashError):
        ok = False
    if not ok:
        warn("auth.login_failed", user=fingerprint(username))
        raise Unauthorized("نام کاربری یا گذرواژه درست نیست.")
    user = rows[0]
    if ph.check_needs_rehash(user["password_hash"]):
        repo.update(
            SYSTEM,
            "admin_users",
            {"password_hash": hash_password(password)},
            where="id = ?",
            params=[user["id"]],
        )
    repo.update(
        SYSTEM, "admin_users", {"last_login_at": _iso(_now())}, where="id = ?", params=[user["id"]]
    )
    return Actor(role=user["role"], user_id=user["id"])


def start_session(actor: Actor, ip: str) -> tuple[str, dt.datetime]:
    sid = secrets.token_hex(32)
    expires = _now() + dt.timedelta(hours=settings.SESSION_TTL_H)
    repo.insert(
        SYSTEM,
        "sessions",
        {
            "id": sid,
            "user_id": actor.user_id,
            "expires_at": _iso(expires),
            "ip_fp": fingerprint(ip),
        },
    )
    info("auth.session_started", user_id=actor.user_id)
    return sid, expires


def resolve_session(sid: str | None) -> Actor:
    from .policies import ANON

    if not sid or len(sid) != 64:
        return ANON
    rows = repo.select(SYSTEM, "sessions", where="id = ?", params=[sid], limit=1)
    if not rows:
        return ANON
    s = rows[0]
    if s["revoked_at"] or s["expires_at"] <= _iso(_now()):
        return ANON
    users = repo.select(
        SYSTEM, "admin_users", where="id = ? AND is_active = 1", params=[s["user_id"]], limit=1
    )
    if not users:
        return ANON
    return Actor(role=users[0]["role"], user_id=users[0]["id"], session_id=sid)


def revoke_session(sid: str) -> None:
    repo.update(SYSTEM, "sessions", {"revoked_at": _iso(_now())}, where="id = ?", params=[sid])


def purge_expired_sessions() -> int:
    return repo.delete(SYSTEM, "sessions", where="expires_at <= ?", params=[_iso(_now())])


def require(actor: Actor, role: str = "staff") -> Actor:
    if actor.role == "anon":
        raise Unauthorized()
    if not actor.at_least(role):  # type: ignore[arg-type]
        raise Forbidden()
    return actor


def csrf_token(session_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), session_id.encode(), "sha256").hexdigest()[:32]


def check_csrf(session_id: str, token: str | None) -> None:
    if not token or not hmac.compare_digest(csrf_token(session_id), token):
        raise Forbidden("توکن امنیتی نامعتبر است. صفحه را تازه کنید.")
