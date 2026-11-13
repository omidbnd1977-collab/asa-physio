"""Owner-managed secretary (staff) accounts — never logs passwords."""

from __future__ import annotations

from typing import Any

from . import audit, auth, repo
from . import permissions as perms
from .errors import BadRequest, Conflict, NotFound
from .logging_ import info
from .policies import SYSTEM, Actor


def _public(row: dict, permissions: frozenset[str] | None = None) -> dict[str, Any]:
    pid = int(row["id"])
    caps = permissions if permissions is not None else auth.load_permissions(pid)
    return {
        "id": pid,
        "username": row["username"],
        "full_name": row.get("full_name") or row["username"],
        "role": row["role"],
        "is_active": bool(row["is_active"]),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "last_login_at": row.get("last_login_at"),
        "permissions": sorted(caps),
    }


def _owned_secretary(owner: Actor, secretary_id: int) -> dict:
    """Only secretaries already owned by this doctor. Orphans are never auto-claimed."""
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="id = ? AND role = 'staff' AND created_by = ?",
        params=[secretary_id, owner.user_id],
        limit=1,
    )
    if not rows:
        raise NotFound("منشی پیدا نشد.")
    return rows[0]


def list_secretaries(owner: Actor) -> list[dict[str, Any]]:
    auth.require(owner, "owner")
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="role = 'staff' AND created_by = ?",
        params=[owner.user_id],
        order_by="created_at desc",
    )
    return [_public(r) for r in rows]


def list_orphan_secretaries(owner: Actor) -> list[dict[str, Any]]:
    """Staff accounts with no owner — visible for explicit assignment only."""
    auth.require(owner, "owner")
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="role = 'staff' AND created_by IS NULL",
        order_by="created_at desc",
    )
    return [_public(r) for r in rows]


def assign_orphan_secretary(owner: Actor, secretary_id: int) -> dict[str, Any]:
    """Explicit claim of an orphan staff account by the current owner."""
    auth.require(owner, "owner")
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="id = ? AND role = 'staff' AND created_by IS NULL",
        params=[secretary_id],
        limit=1,
    )
    if not rows:
        raise NotFound("منشی بدون مالک پیدا نشد.")
    n = repo.update(
        SYSTEM,
        "admin_users",
        {"created_by": owner.user_id},
        where="id = ? AND created_by IS NULL AND role = 'staff'",
        params=[secretary_id],
    )
    if n != 1:
        raise Conflict("این حساب هم‌اکنون به مالک دیگری اختصاص یافته است.")
    audit.record(
        owner,
        "secretary.assigned",
        "admin_user",
        secretary_id,
        before={"created_by": None},
        after={"created_by": owner.user_id, "username": rows[0]["username"]},
    )
    info("secretary.assigned", secretary_id=secretary_id, by=owner.user_id)
    fresh = repo.select(SYSTEM, "admin_users", where="id = ?", params=[secretary_id], limit=1)[0]
    return _public(fresh)


def create_secretary(
    owner: Actor,
    *,
    full_name: str,
    username: str,
    password: str,
    permissions: list[str],
) -> dict[str, Any]:
    auth.require(owner, "owner")
    name = full_name.strip()
    user = username.strip().lower()
    if len(name) < 3:
        raise BadRequest("نام و نام خانوادگی را کامل وارد کنید.")
    if not (3 <= len(user) <= 32) or not user.replace("_", "").isalnum():
        raise BadRequest("نام کاربری باید ۳ تا ۳۲ نویسه و فقط حروف، رقم یا _ باشد.")
    if len(password) < auth.STAFF_PASSWORD_MIN:
        raise BadRequest(
            f"رمز عبور اولیه باید دست‌کم {auth.STAFF_PASSWORD_MIN} نویسه باشد."
        )
    if len(password) > 256:
        raise BadRequest("رمز عبور بیش از حد طولانی است.")
    caps = perms.normalize(permissions)
    existing = repo.select(
        SYSTEM, "admin_users", where="username = ?", params=[user], limit=1
    )
    if existing:
        raise Conflict("این نام کاربری قبلاً ثبت شده است.")
    try:
        hashed = auth.hash_password(password, min_length=auth.STAFF_PASSWORD_MIN)
    except ValueError as exc:
        raise BadRequest("رمز عبور اولیه معتبر نیست.") from exc
    uid = repo.insert(
        SYSTEM,
        "admin_users",
        {
            "username": user,
            "password_hash": hashed,
            "full_name": name[:80],
            "role": "staff",
            "created_by": owner.user_id,
        },
    )
    info("auth.user_created", user_id=uid, role="staff")
    auth.replace_permissions(uid, caps)
    row = repo.select(SYSTEM, "admin_users", where="id = ?", params=[uid], limit=1)[0]
    out = _public(row, caps)
    audit.record(
        owner,
        "secretary.created",
        "admin_user",
        uid,
        after={"username": user, "full_name": name, "permissions": sorted(caps)},
    )
    info("secretary.created", secretary_id=uid, by=owner.user_id)
    return out


def update_secretary(
    owner: Actor,
    secretary_id: int,
    *,
    full_name: str | None = None,
    is_active: bool | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    auth.require(owner, "owner")
    row = _owned_secretary(owner, secretary_id)
    before = _public(row)
    patch: dict[str, Any] = {}
    if full_name is not None:
        name = full_name.strip()
        if len(name) < 3:
            raise BadRequest("نام و نام خانوادگی را کامل وارد کنید.")
        patch["full_name"] = name[:80]
    if is_active is not None:
        patch["is_active"] = 1 if is_active else 0
    if patch:
        repo.update(SYSTEM, "admin_users", patch, where="id = ?", params=[secretary_id])
    caps = auth.load_permissions(secretary_id)
    if permissions is not None:
        caps = auth.replace_permissions(secretary_id, permissions)
    if is_active is False:
        repo.update(
            SYSTEM,
            "sessions",
            {"revoked_at": auth._iso(auth._now())},
            where="user_id = ? AND revoked_at IS NULL",
            params=[secretary_id],
        )
    fresh = repo.select(SYSTEM, "admin_users", where="id = ?", params=[secretary_id], limit=1)[0]
    out = _public(fresh, caps)
    audit.record(
        owner,
        "secretary.updated",
        "admin_user",
        secretary_id,
        before={
            "full_name": before["full_name"],
            "is_active": before["is_active"],
            "permissions": before["permissions"],
        },
        after={
            "full_name": out["full_name"],
            "is_active": out["is_active"],
            "permissions": out["permissions"],
        },
    )
    return out


def set_secretary_password(owner: Actor, secretary_id: int, password: str) -> dict[str, Any]:
    auth.require(owner, "owner")
    _owned_secretary(owner, secretary_id)
    if len(password) < auth.STAFF_PASSWORD_MIN:
        raise BadRequest(
            f"رمز عبور باید دست‌کم {auth.STAFF_PASSWORD_MIN} نویسه باشد."
        )
    if len(password) > 256:
        raise BadRequest("رمز عبور بیش از حد طولانی است.")
    try:
        hashed = auth.hash_password(password, min_length=auth.STAFF_PASSWORD_MIN)
    except ValueError as exc:
        raise BadRequest("رمز عبور معتبر نیست.") from exc
    repo.update(
        SYSTEM,
        "admin_users",
        {"password_hash": hashed},
        where="id = ?",
        params=[secretary_id],
    )
    repo.update(
        SYSTEM,
        "sessions",
        {"revoked_at": auth._iso(auth._now())},
        where="user_id = ? AND revoked_at IS NULL",
        params=[secretary_id],
    )
    audit.record(
        owner,
        "secretary.password_reset",
        "admin_user",
        secretary_id,
        after={"password_reset": True},
    )
    info("secretary.password_reset", secretary_id=secretary_id, by=owner.user_id)
    return {"ok": True}


def _secretary_has_history(secretary_id: int) -> bool:
    """True when deleting the row would lose booker/audit/clinical attribution."""
    if repo.count(
        SYSTEM, "appointments", where="booked_by_user_id = ?", params=[secretary_id]
    ):
        return True
    if repo.count(
        SYSTEM,
        "audit_logs",
        where="actor_role = 'staff' AND actor_id = ?",
        params=[secretary_id],
    ):
        return True
    if repo.count(
        SYSTEM, "treatment_sessions", where="created_by = ?", params=[secretary_id]
    ):
        return True
    return bool(
        repo.count(SYSTEM, "messages", where="created_by = ?", params=[secretary_id])
    )


def delete_secretary(owner: Actor, secretary_id: int) -> dict[str, Any]:
    """Hard-delete only when safe; otherwise soft-deactivate and revoke sessions."""
    auth.require(owner, "owner")
    row = _owned_secretary(owner, secretary_id)
    before = _public(row)
    repo.update(
        SYSTEM,
        "sessions",
        {"revoked_at": auth._iso(auth._now())},
        where="user_id = ? AND revoked_at IS NULL",
        params=[secretary_id],
    )
    if _secretary_has_history(secretary_id):
        repo.update(
            SYSTEM,
            "admin_users",
            {"is_active": 0},
            where="id = ? AND role = 'staff' AND created_by = ?",
            params=[secretary_id, owner.user_id],
        )
        audit.record(
            owner,
            "secretary.soft_deleted",
            "admin_user",
            secretary_id,
            before={"is_active": before["is_active"], "username": before["username"]},
            after={"is_active": False, "mode": "soft"},
        )
        info("secretary.soft_deleted", secretary_id=secretary_id, by=owner.user_id)
        return {
            "ok": True,
            "mode": "soft",
            "id": secretary_id,
            "message": "منشی سابقه دارد؛ حساب غیرفعال شد تا سوابق نوبت و Audit حفظ شود.",
        }
    auth.replace_permissions(secretary_id, [])
    repo.delete(SYSTEM, "admin_users", where="id = ? AND role = 'staff'", params=[secretary_id])
    audit.record(
        owner,
        "secretary.deleted",
        "admin_user",
        secretary_id,
        before={"username": before["username"], "full_name": before["full_name"]},
        after={"mode": "hard"},
    )
    info("secretary.deleted", secretary_id=secretary_id, by=owner.user_id)
    return {
        "ok": True,
        "mode": "hard",
        "id": secretary_id,
        "message": "منشی حذف شد.",
    }


def list_orphan_patients(owner: Actor) -> dict[str, Any]:
    auth.require(owner, "owner")
    rows = repo.select(
        SYSTEM,
        "patients",
        where="owner_id IS NULL",
        order_by="created_at desc",
        limit=500,
    )
    return {
        "total": repo.count(SYSTEM, "patients", where="owner_id IS NULL"),
        "items": [
            {
                "public_id": r["public_id"],
                "full_name": r["full_name"],
                "phone": r["phone"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


def assign_orphan_patients(owner: Actor, public_ids: list[str]) -> dict[str, Any]:
    """Assign explicitly selected unassigned patients to the current owner."""
    auth.require(owner, "owner")
    ids = [str(p)[:12] for p in public_ids if p]
    if not ids:
        raise BadRequest("هیچ بیماری برای اختصاص انتخاب نشده است.")
    if len(ids) > 200:
        raise BadRequest("حداکثر ۲۰۰ بیمار در هر درخواست.")
    assigned: list[str] = []
    with repo.tx() as conn:
        for pid in ids:
            cur = conn.execute(
                "UPDATE patients SET owner_id = ? WHERE public_id = ? AND owner_id IS NULL",
                (owner.user_id, pid),
            )
            if cur.rowcount == 1:
                assigned.append(pid)
        if not assigned:
            raise NotFound("هیچ بیمار بدون مالکی با شناسه‌های داده‌شده پیدا نشد.")
        for pid in assigned:
            audit.record(
                owner,
                "patient.owner_assigned",
                "patient",
                pid,
                before={"owner_id": None},
                after={"owner_id": owner.user_id},
            )
    info("patients.assigned", count=len(assigned), by=owner.user_id)
    return {"ok": True, "assigned": assigned, "count": len(assigned)}
