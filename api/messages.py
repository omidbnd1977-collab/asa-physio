"""Doctor <-> patient messaging: the in-app channel that ties the two panels together.

Everything the clinic does to an appointment (book, move, cancel, next session, a
recorded treatment) also leaves a short note in the patient's thread, so the portal
and the doctor panel always tell the same story. On top of those notes, both sides
can simply write to each other.

Reads and writes by patients go through the SYSTEM actor, always scoped to their own
`patient_id` — the `messages` table policy is what keeps the rest unreachable.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from . import repo
from .logging_ import info
from .policies import SYSTEM, Actor
from .schemas import clean_text

CHAT = "chat"


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _view(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "public_id": r["public_id"],
        "sender": r["sender"],
        "kind": r["kind"],
        "body": r["body"],
        "created_at": r["created_at"],
        "read": bool(r["read_by_patient_at"]) if r["sender"] != "patient" else None,
    }


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def post(
    patient_id: int,
    sender: str,
    body: str,
    *,
    kind: str = CHAT,
    appointment_id: int | None = None,
    created_by: int | None = None,
) -> dict[str, Any]:
    """Store one message. Text is cleaned here as a second line of defence."""
    text = clean_text(body, max_len=1000)
    if len(text) < 2:
        from .errors import BadRequest

        raise BadRequest("متن پیام خیلی کوتاه است.", fields={"body": "خیلی کوتاه"})
    public_id = secrets.token_hex(6)
    repo.insert(
        SYSTEM,
        "messages",
        {
            "public_id": public_id,
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "sender": sender,
            "kind": kind,
            "body": text,
            "created_by": created_by,
            # a patient's own words are already read by the patient, and vice versa
            "read_by_patient_at": _now() if sender == "patient" else None,
            "read_by_staff_at": _now() if sender != "patient" else None,
        },
    )
    info("messages.posted", patient_id=patient_id, sender=sender, kind=kind)
    return {"public_id": public_id, "body": text, "sender": sender, "kind": kind}


def system_note(
    patient_id: int,
    body: str,
    *,
    kind: str,
    appointment_id: int | None = None,
    from_patient: bool = False,
) -> None:
    """An automatic note about something that just happened to an appointment.

    `from_patient=True` attributes the note to the patient (e.g. a cancellation the
    patient made in the portal) so it also lights up the staff inbox badge.
    """
    post(
        patient_id,
        "patient" if from_patient else "system",
        body,
        kind=kind,
        appointment_id=appointment_id,
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def thread(patient_id: int, limit: int = 200) -> list[dict[str, Any]]:
    rows = repo.select(
        SYSTEM,
        "messages",
        where="patient_id = ?",
        params=[patient_id],
        order_by="created_at desc",
        limit=limit,
    )
    rows.sort(key=lambda r: (r["created_at"], r["id"]))
    return [_view(r) for r in rows]


def mark_read_by_patient(patient_id: int) -> int:
    return repo.update(
        SYSTEM,
        "messages",
        {"read_by_patient_at": _now()},
        where="patient_id = ? AND sender != 'patient' AND read_by_patient_at IS NULL",
        params=[patient_id],
    )


def mark_read_by_staff(patient_id: int) -> int:
    return repo.update(
        SYSTEM,
        "messages",
        {"read_by_staff_at": _now()},
        where="patient_id = ? AND sender = 'patient' AND read_by_staff_at IS NULL",
        params=[patient_id],
    )


def unread_for_patient(patient_id: int) -> int:
    return repo.count(
        SYSTEM,
        "messages",
        where="patient_id = ? AND sender IN ('staff','system') AND read_by_patient_at IS NULL",
        params=[patient_id],
    )


def unread_for_staff() -> int:
    return repo.count(SYSTEM, "messages", where="sender = 'patient' AND read_by_staff_at IS NULL")


def inbox(actor: Actor, limit: int = 300) -> list[dict[str, Any]]:
    """One row per patient: the latest message and how many are still unread."""
    rows = repo.select(actor, "messages", order_by="created_at desc", limit=limit)
    # created_at has one-second resolution — the id breaks the tie deterministically
    rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    latest: dict[int, dict[str, Any]] = {}
    unread: dict[int, int] = {}
    for r in rows:
        latest.setdefault(r["patient_id"], r)
        if r["sender"] == "patient" and not r["read_by_staff_at"]:
            unread[r["patient_id"]] = unread.get(r["patient_id"], 0) + 1
    if not latest:
        return []
    marks = ",".join("?" for _ in latest)
    pmap = {
        p["id"]: p
        for p in repo.select(actor, "patients", where=f"id IN ({marks})", params=list(latest))
    }
    out = []
    for pid, r in latest.items():
        p = pmap.get(pid)
        if not p:
            continue
        out.append(
            {
                "patient": {
                    "public_id": p["public_id"],
                    "full_name": p["full_name"],
                    "phone": p["phone"],
                },
                "last": _view(r),
                "unread": unread.get(pid, 0),
            }
        )
    # unread threads first, newest message first inside each group (stable sorts)
    out.sort(key=lambda x: x["last"]["created_at"], reverse=True)
    out.sort(key=lambda x: x["unread"], reverse=True)
    return out
