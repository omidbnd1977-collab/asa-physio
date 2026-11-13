"""Layer 04 + 08 — password hashing, sessions, and actor resolution."""

from __future__ import annotations

import datetime as dt
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from . import permissions as perms
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


def load_permissions(user_id: int) -> frozenset[str]:
    rows = repo.select(
        SYSTEM,
        "staff_permissions",
        where="user_id = ?",
        params=[user_id],
    )
    return frozenset(str(r["permission"]) for r in rows)


def actor_from_user(user: dict, *, session_id: str | None = None) -> Actor:
    role = user["role"]
    permissions = frozenset()
    if role == "staff":
        permissions = load_permissions(int(user["id"]))
    return Actor(
        role=role,
        user_id=int(user["id"]),
        session_id=session_id,
        permissions=permissions,
    )


def create_user(
    username: str,
    password: str,
    role: str = "staff",
    *,
    full_name: str = "",
    created_by: int | None = None,
) -> int:
    uid = repo.insert(
        SYSTEM,
        "admin_users",
        {
            "username": username.strip().lower(),
            "password_hash": hash_password(password),
            "full_name": (full_name or username).strip()[:80],
            "role": role,
            "created_by": created_by,
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
    # Orphan secretaries (no owning doctor) cannot use the panel until explicitly assigned.
    if user["role"] == "staff" and user.get("created_by") is None:
        warn("auth.login_orphan_staff", user=fingerprint(username))
        raise Unauthorized("نام کاربری یا گذرواژه درست نیست.")
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
    return actor_from_user(user)


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
    user = users[0]
    if user["role"] == "staff" and user.get("created_by") is None:
        return ANON
    return actor_from_user(user, session_id=sid)


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


def require_perm(actor: Actor, permission: str) -> Actor:
    """Staff must hold the permission; owners always pass. Call after require(staff)."""
    require(actor, "staff")
    if not actor.can(permission):
        raise Forbidden("اجازهٔ دسترسی به این بخش را ندارید.")
    return actor


def replace_permissions(user_id: int, permissions: list[str] | set[str]) -> frozenset[str]:
    wanted = perms.normalize(permissions)
    with repo.tx() as conn:
        conn.execute("DELETE FROM staff_permissions WHERE user_id = ?", (user_id,))
        for p in sorted(wanted):
            conn.execute(
                "INSERT INTO staff_permissions(user_id, permission) VALUES (?, ?)",
                (user_id, p),
            )
    return wanted


def csrf_token(session_id: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), session_id.encode(), "sha256").hexdigest()[:32]


def check_csrf(session_id: str, token: str | None) -> None:
    if not token or not hmac.compare_digest(csrf_token(session_id), token):
        raise Forbidden("توکن امنیتی نامعتبر است. صفحه را تازه کنید.")
