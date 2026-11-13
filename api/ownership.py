"""Clinic ownership scope — secretaries only see their doctor's patients."""

from __future__ import annotations

from typing import Any

from . import repo
from .errors import Forbidden, NotFound
from .policies import SYSTEM, Actor


def clinic_owner_id(actor: Actor) -> int | None:
    """Owning doctor id for this actor, or None if the actor has no clinic scope."""
    if actor.role == "owner" and actor.user_id is not None:
        return int(actor.user_id)
    if actor.role == "staff" and actor.user_id is not None:
        rows = repo.select(
            SYSTEM,
            "admin_users",
            where="id = ? AND role = 'staff' AND is_active = 1",
            params=[actor.user_id],
            limit=1,
        )
        if not rows:
            return None
        created_by = rows[0].get("created_by")
        return int(created_by) if created_by is not None else None
    return None


def sole_active_owner_id() -> int | None:
    """If exactly one active owner exists, return their id (portal auto-attach)."""
    rows = repo.select(
        SYSTEM,
        "admin_users",
        where="role = 'owner' AND is_active = 1",
        columns=["id"],
        limit=3,
    )
    if len(rows) == 1:
        return int(rows[0]["id"])
    return None


def owned_patients_where(actor: Actor) -> tuple[str, list[Any]]:
    """SQL fragment limiting patients to this actor's clinic. Never includes NULL owner_id."""
    oid = clinic_owner_id(actor)
    if oid is None:
        return ("0=1", [])
    return ("owner_id = ?", [oid])


def get_owned_patient(actor: Actor, public_id: str) -> dict:
    """Load a patient in-scope or raise NotFound (no existence leak across owners)."""
    oid = clinic_owner_id(actor)
    if oid is None:
        raise Forbidden("حساب شما به هیچ کلینیکی متصل نیست.")
    rows = repo.select(
        SYSTEM,
        "patients",
        where="public_id = ? AND owner_id = ?",
        params=[public_id[:12], oid],
        limit=1,
    )
    if not rows:
        raise NotFound("بیمار پیدا نشد.")
    return dict(rows[0])


def get_owned_appointment(actor: Actor, public_id: str) -> dict:
    oid = clinic_owner_id(actor)
    if oid is None:
        raise Forbidden("حساب شما به هیچ کلینیکی متصل نیست.")
    rows = repo.select(
        SYSTEM,
        "appointments",
        where=(
            "public_id = ? AND patient_id IN "
            "(SELECT id FROM patients WHERE owner_id = ?)"
        ),
        params=[public_id[:10], oid],
        limit=1,
    )
    if not rows:
        raise NotFound("این نوبت پیدا نشد.")
    return dict(rows[0])


def assert_patient_row_owned(actor: Actor, patient: dict) -> None:
    oid = clinic_owner_id(actor)
    prow_owner = patient.get("owner_id")
    if oid is None or prow_owner is None or int(prow_owner) != oid:
        raise NotFound("بیمار پیدا نشد.")


def can_manage_reception_queue(actor: Actor) -> bool:
    """Owner or staff holding reception.queue may see/manage unassigned queues."""
    from . import permissions as perms

    if actor.role == "owner":
        return True
    return actor.role == "staff" and actor.can(perms.RECEPTION_QUEUE)


def bookings_visible_where(actor: Actor) -> tuple[str, list[Any]]:
    """Assigned leads for the clinic; unassigned only with reception.queue / owner."""
    from . import permissions as perms

    oid = clinic_owner_id(actor)
    if actor.role == "owner" and actor.user_id is not None:
        # Owner sees own assigned leads + the shared unassigned queue.
        return ("owner_id = ? OR owner_id IS NULL", [actor.user_id])
    if oid is None:
        return ("0=1", [])
    if actor.can(perms.RECEPTION_QUEUE):
        return ("owner_id = ? OR owner_id IS NULL", [oid])
    if actor.can(perms.MESSAGES_VIEW):
        return ("owner_id = ?", [oid])
    return ("0=1", [])


def inbound_visible_where(actor: Actor) -> tuple[str, list[Any]]:
    """Inbound SMS: assigned to clinic patients; unassigned only for reception/owner."""
    from . import permissions as perms

    oid = clinic_owner_id(actor)
    if actor.role == "owner" and actor.user_id is not None:
        return ("owner_id = ? OR owner_id IS NULL", [actor.user_id])
    if oid is None:
        return ("0=1", [])
    if actor.can(perms.RECEPTION_QUEUE):
        return ("owner_id = ? OR owner_id IS NULL", [oid])
    # Matched inbound tied to owned patients (no public queue).
    return (
        "owner_id = ? OR patient_id IN (SELECT id FROM patients WHERE owner_id = ?)",
        [oid, oid],
    )
