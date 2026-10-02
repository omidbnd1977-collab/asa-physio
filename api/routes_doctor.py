"""Doctor/staff API for the patient portal: patients, appointments, reschedule,
next session, attendance, and the SMS log."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, File, Request, Response, UploadFile
from fastapi.responses import FileResponse

from . import auth, cache, clinical, metrics, patients, repo, scheduling, schemas, sms
from .errors import BadRequest, Conflict, NotFound
from .jalali import now_tehran, to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM, Actor

router = APIRouter(prefix="/api/admin", tags=["doctor"])


def staff(request: Request) -> Actor:
    return auth.require(auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE)), "staff")


def csrf(request: Request, actor: Actor) -> None:
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def full_appt(a: dict, p: dict | None = None) -> dict[str, Any]:
    out = {
        "public_id": a["public_id"],
        "slot_date": a["slot_date"],
        "slot_time": a["slot_time"],
        "jalali": to_jalali_str(a["slot_date"]),
        "label": to_jalali_long(a["slot_date"]),
        "cabin": a["cabin"],
        "status": a["status"],
        "kind": a["kind"],
        "booked_by": a["booked_by"],
        "attendance": a["attendance"],
        "confirm_sent_at": a["confirm_sent_at"],
        "confirmed_at": a["confirmed_at"],
        "moved_from": a["moved_from"],
        "note": a["note"],
        "created_at": a["created_at"],
    }
    if p:
        out["patient"] = {
            "public_id": p["public_id"],
            "full_name": p["full_name"],
            "phone": p["phone"],
            "national_id": p["national_id"],
            "birth_jalali": p["birth_jalali"],
            "mri_link": p["mri_link"],
            "ortho_doctor": p["ortho_doctor"],
            "med_photo": p["med_photo"],
            "mri_file": p["mri_file"],
            "staff_note": p["staff_note"],
        }
    return out


def _patient_map(actor: Actor, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = repo.select(actor, "patients", where=f"id IN ({marks})", params=ids)
    return {r["id"]: r for r in rows}


# --------------------------------------------------------------------------
# appointments
# --------------------------------------------------------------------------
@router.get("/appointments")
async def list_appointments(
    request: Request,
    response: Response,
    date: str = "",
    scope: str = "upcoming",
    page: int = 1,
    per_page: int = 50,
) -> Any:
    actor = staff(request)
    page = max(1, min(page, 200))
    per_page = max(1, min(per_page, 200))
    today = now_tehran().date().isoformat()

    if date:
        where, params = "slot_date = ?", [date]
    elif scope == "today":
        where, params = "slot_date = ?", [today]
    elif scope == "past":
        where, params = "slot_date < ?", [today]
    elif scope == "all":
        where, params = "1=1", []
    else:
        where, params = "slot_date >= ?", [today]

    key = f"admin:appointments:{where}:{params}:{page}:{per_page}"

    def build() -> dict[str, Any]:
        rows = repo.select(
            actor,
            "appointments",
            where=where,
            params=params,
            order_by="slot_date",
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        rows.sort(key=lambda a: (a["slot_date"], a["slot_time"]))
        pmap = _patient_map(actor, [r["patient_id"] for r in rows])
        return {
            "items": [full_appt(a, pmap.get(a["patient_id"])) for a in rows],
            "total": repo.count(actor, "appointments", where=where, params=params),
            "page": page,
            "per_page": per_page,
            "today": today,
            "today_label": to_jalali_long(today),
        }

    data = cache.memo(key, 10, build)
    tag = cache.etag_for(data)
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    response.headers["ETag"] = tag
    response.headers["Cache-Control"] = "private, max-age=5"
    return data


@router.get("/appointments/grid")
async def grid(request: Request, date: str = "") -> dict[str, Any]:
    """The same 12x10 grid the patient sees, so staff can place someone by hand."""
    actor = staff(request)
    d = date or now_tehran().date().isoformat()
    g = scheduling.day_grid(d, for_patient=False)
    rows = repo.select(
        actor, "appointments", where="slot_date = ? AND status != 'cancelled'", params=[d]
    )
    pmap = _patient_map(actor, [r["patient_id"] for r in rows])
    by_slot: dict[str, list[dict]] = {}
    for a in rows:
        p = pmap.get(a["patient_id"], {})
        by_slot.setdefault(a["slot_time"], []).append(
            {
                "public_id": a["public_id"],
                "cabin": a["cabin"],
                "status": a["status"],
                "attendance": a["attendance"],
                "name": p.get("full_name", "—"),
                "phone": p.get("phone", ""),
            }
        )
    g["occupants"] = by_slot
    return g


@router.patch("/appointments/{public_id}/move")
async def move(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Reschedule. The patient is told by SMS."""
    actor = staff(request)
    csrf(request, actor)
    data = schemas.AppointmentMoveIn.model_validate(payload)

    rows = repo.select(
        actor, "appointments", where="public_id = ?", params=[public_id[:10]], limit=1
    )
    if not rows:
        raise NotFound("این نوبت پیدا نشد.")
    old = rows[0]
    if old["status"] == "cancelled":
        raise Conflict("این نوبت لغو شده است.")
    if old["slot_date"] == data.slot_date and old["slot_time"] == data.slot_time:
        raise BadRequest("زمان جدید با زمان فعلی یکی است.")

    p = repo.select(actor, "patients", where="id = ?", params=[old["patient_id"]], limit=1)
    if not p:
        raise NotFound("بیمار پیدا نشد.")
    patient = p[0]

    old_when = scheduling.describe(old)
    # free the old slot first so a move inside the same half hour can reuse the cabin
    repo.update(
        SYSTEM,
        "appointments",
        {"status": "cancelled", "updated_at": _now()},
        where="id = ?",
        params=[old["id"]],
    )
    try:
        new = scheduling.allocate(
            patient["id"],
            data.slot_date,
            data.slot_time,
            kind=old["kind"],
            booked_by="staff",
            note=old["note"],
            moved_from=f"{old['slot_date']} {old['slot_time']}",
        )
    except Exception:
        repo.update(
            SYSTEM,
            "appointments",
            {"status": old["status"], "updated_at": _now()},
            where="id = ?",
            params=[old["id"]],
        )
        raise

    new_when = scheduling.describe(new)
    sent = False
    if data.notify:
        _, sent = sms.send_now(
            patient["phone"],
            sms.body_rescheduled(patient["full_name"], old_when, new_when),
            "rescheduled",
            patient_id=patient["id"],
            appointment_id=new["id"],
        )
    cache.purge("admin:appointments")
    info("doctor.moved", from_id=old["public_id"], to_id=new["public_id"], sms_sent=sent)
    return {
        "ok": True,
        "sms_sent": sent,
        "code": new["public_id"],
        "old": old_when,
        "new": new_when,
        "cabin": new["cabin"],
    }


@router.post("/patients/{public_id}/followup", status_code=201)
async def followup(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Book the next session for a patient. The patient is told by SMS."""
    actor = staff(request)
    csrf(request, actor)
    data = schemas.FollowUpIn.model_validate(payload)
    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("بیمار پیدا نشد.")
    patient = rows[0]
    appt = scheduling.allocate(
        patient["id"],
        data.slot_date,
        data.slot_time,
        kind="followup",
        booked_by="staff",
        note=data.note,
    )
    when = scheduling.describe(appt)
    gap = clinical.returning_after_gap(patient["id"], data.slot_date)
    sent = welcomed = False
    if data.notify:
        if gap:
            _, welcomed = sms.send_now(
                patient["phone"],
                sms.body_welcome_back(patient["full_name"], when, gap),
                "welcome_back",
                patient_id=patient["id"],
                appointment_id=appt["id"],
            )
        _, sent = sms.send_now(
            patient["phone"],
            sms.body_followup(patient["full_name"], when),
            "followup",
            patient_id=patient["id"],
            appointment_id=appt["id"],
        )
    metrics.record("followup_booked")
    cache.purge("admin:appointments")
    return {
        "ok": True,
        "sms_sent": sent,
        "welcomed_back": welcomed,
        "gap_days": gap,
        "code": appt["public_id"],
        "when": when,
        "cabin": appt["cabin"],
    }


@router.patch("/appointments/{public_id}/status")
async def set_status(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    data = schemas.AppointmentStatusIn.model_validate(payload)
    rows = repo.select(
        actor, "appointments", where="public_id = ?", params=[public_id[:10]], limit=1
    )
    if not rows:
        raise NotFound("این نوبت پیدا نشد.")
    appt = rows[0]
    repo.update(
        actor,
        "appointments",
        {"status": data.status, "updated_at": _now()},
        where="public_id = ?",
        params=[public_id[:10]],
    )
    sent = False
    if data.status == "cancelled" and data.notify:
        p = repo.select(actor, "patients", where="id = ?", params=[appt["patient_id"]], limit=1)
        if p:
            _, sent = sms.send_now(
                p[0]["phone"],
                sms.body_cancelled(p[0]["full_name"], scheduling.describe(appt)),
                "cancelled",
                patient_id=p[0]["id"],
                appointment_id=appt["id"],
            )
    if data.status == "attended":
        metrics.record("session_attended")
    cache.purge("admin:appointments")
    return {"ok": True, "status": data.status, "sms_sent": sent}


@router.post("/appointments/{public_id}/remind")
async def remind(public_id: str, request: Request) -> dict[str, Any]:
    """Send the confirmation request by hand, without waiting for the scheduler."""
    actor = staff(request)
    csrf(request, actor)
    rows = repo.select(
        actor, "appointments", where="public_id = ?", params=[public_id[:10]], limit=1
    )
    if not rows:
        raise NotFound("این نوبت پیدا نشد.")
    appt = rows[0]
    p = repo.select(actor, "patients", where="id = ?", params=[appt["patient_id"]], limit=1)
    if not p:
        raise NotFound("بیمار پیدا نشد.")
    _, sent = sms.send_now(
        p[0]["phone"],
        sms.body_confirm(p[0]["full_name"], scheduling.describe(appt)),
        "confirm_request",
        patient_id=p[0]["id"],
        appointment_id=appt["id"],
    )
    repo.update(
        SYSTEM, "appointments", {"confirm_sent_at": _now()}, where="id = ?", params=[appt["id"]]
    )
    cache.purge("admin:appointments")
    return {"ok": True, "sms_sent": sent}


# --------------------------------------------------------------------------
# patients
# --------------------------------------------------------------------------
@router.get("/patients")
async def list_patients(
    request: Request, q: str = "", page: int = 1, per_page: int = 20
) -> dict[str, Any]:
    actor = staff(request)
    page = max(1, min(page, 500))
    per_page = max(1, min(per_page, 100))
    if q:
        term = f"%{q.strip()[:40]}%"
        where = "(full_name LIKE ? OR phone LIKE ? OR national_id LIKE ?)"
        params = [term, term, term]
    else:
        where, params = "1=1", []
    rows = repo.select(
        actor,
        "patients",
        where=where,
        params=params,
        order_by="created_at desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    out = []
    for p in rows:
        appts = repo.select(
            actor,
            "appointments",
            where="patient_id = ? AND status != 'cancelled'",
            params=[p["id"]],
            order_by="slot_date desc",
            limit=50,
        )
        appts.sort(key=lambda a: (a["slot_date"], a["slot_time"]))
        out.append(
            {
                "public_id": p["public_id"],
                "full_name": p["full_name"],
                "national_id": p["national_id"],
                "phone": p["phone"],
                "birth_jalali": p["birth_jalali"],
                "mri_link": p["mri_link"],
                "ortho_doctor": p["ortho_doctor"],
                "med_photo": p["med_photo"],
                "mri_file": p["mri_file"],
                "staff_note": p["staff_note"],
                "created_at": p["created_at"],
                "appointments": [full_appt(a) for a in appts],
                "history": clinical.history(actor, p["id"], limit=20),
                "clinical": clinical.summary(actor, p["id"]),
            }
        )
    return {
        "items": out,
        "page": page,
        "per_page": per_page,
        "total": repo.count(actor, "patients", where=where, params=params),
    }


@router.patch("/patients/{public_id}/note")
async def patient_note(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    data = schemas.PatientNoteIn.model_validate(payload)
    n = repo.update(
        actor,
        "patients",
        {"staff_note": data.staff_note, "updated_at": _now()},
        where="public_id = ?",
        params=[public_id[:12]],
    )
    if not n:
        raise NotFound("بیمار پیدا نشد.")
    return {"ok": True}


@router.get("/patients/{public_id}/photo")
async def patient_photo(public_id: str, request: Request) -> Response:
    """Medication photo. Staff only — uploads are never served from a public path."""
    actor = staff(request)
    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows or not rows[0]["med_photo"]:
        raise NotFound("فایلی ثبت نشده است.")
    path = patients.upload_path(rows[0]["med_photo"])
    if path is None:
        raise NotFound("فایل پیدا نشد.")
    return FileResponse(
        path, headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"}
    )


# --------------------------------------------------------------------------
# clinical catalogues  (extensible: type a missing entry once and it stays)
# --------------------------------------------------------------------------
@router.get("/catalog")
async def get_catalog(request: Request) -> dict[str, Any]:
    actor = staff(request)
    return cache.memo("admin:catalog", 30, lambda: clinical.catalog(actor))


@router.post("/catalog/body-parts", status_code=201)
async def add_body_part(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    data = schemas.CatalogItemIn.model_validate(payload)
    out = clinical.add_catalog_item(actor, "body_parts", data.name, data.category)
    cache.purge("admin:catalog")
    return out


@router.post("/catalog/treatments", status_code=201)
async def add_treatment(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    data = schemas.CatalogItemIn.model_validate(payload)
    out = clinical.add_catalog_item(actor, "treatments", data.name)
    cache.purge("admin:catalog")
    return out


# --------------------------------------------------------------------------
# treatment history
# --------------------------------------------------------------------------
@router.get("/patients/{public_id}/sessions")
async def patient_history(public_id: str, request: Request) -> dict[str, Any]:
    actor = staff(request)
    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("بیمار پیدا نشد.")
    pid = rows[0]["id"]
    return {"history": clinical.history(actor, pid), "summary": clinical.summary(actor, pid)}


@router.post("/patients/{public_id}/sessions", status_code=201)
async def record_treatment(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """The doctor records what was done: affected area, treatment performed, notes."""
    actor = staff(request)
    csrf(request, actor)
    data = schemas.TreatmentSessionIn.model_validate(payload)

    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("بیمار پیدا نشد.")
    patient = rows[0]

    appt = None
    if data.appointment_id:
        a = repo.select(
            actor,
            "appointments",
            where="public_id = ? AND patient_id = ?",
            params=[data.appointment_id, patient["id"]],
            limit=1,
        )
        appt = a[0] if a else None

    out = clinical.record_session(
        actor,
        patient["id"],
        body_part_ids=data.body_part_ids,
        treatment_ids=data.treatment_ids,
        findings=data.findings,
        plan=data.plan,
        pain_before=data.pain_before,
        pain_after=data.pain_after,
        appointment_id=appt["id"] if appt else None,
        session_date=data.session_date or (appt["slot_date"] if appt else None),
    )

    if appt and data.mark_attended and appt["status"] == "booked":
        repo.update(
            actor,
            "appointments",
            {"status": "attended", "updated_at": _now()},
            where="id = ?",
            params=[appt["id"]],
        )
        metrics.record("session_attended")

    sent = False
    if data.notify:
        names = [t["name"] for t in clinical._labels(actor, "treatments", data.treatment_ids)]
        when = scheduling.describe(appt) if appt else to_jalali_long(out["session_date"])
        _, sent = sms.send_now(
            patient["phone"],
            sms.body_session_logged(patient["full_name"], when, "، ".join(names)),
            "session_logged",
            patient_id=patient["id"],
            appointment_id=appt["id"] if appt else None,
        )

    cache.purge("admin:appointments")
    info("doctor.session_recorded", session=out["public_id"], sms_sent=sent)
    return {"ok": True, "sms_sent": sent, **out}


# --------------------------------------------------------------------------
# MRI file — uploaded by the patient or by staff, viewable only by staff
# --------------------------------------------------------------------------
@router.post("/patients/{public_id}/mri", status_code=201)
async def upload_mri(
    public_id: str, request: Request, mri_file: UploadFile = File(...)
) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows:
        raise NotFound("بیمار پیدا نشد.")
    name = patients.save_upload(mri_file, prefix=f"mri-{public_id[:6]}")
    if not name:
        raise BadRequest("فایلی دریافت نشد.")
    repo.update(
        actor,
        "patients",
        {"mri_file": name, "updated_at": _now()},
        where="id = ?",
        params=[rows[0]["id"]],
    )
    return {"ok": True, "file": name}


@router.get("/patients/{public_id}/mri")
async def view_mri(public_id: str, request: Request) -> Response:
    actor = staff(request)
    rows = repo.select(actor, "patients", where="public_id = ?", params=[public_id[:12]], limit=1)
    if not rows or not rows[0]["mri_file"]:
        raise NotFound("فایل MRI ثبت نشده است.")
    path = patients.upload_path(rows[0]["mri_file"])
    if path is None:
        raise NotFound("فایل پیدا نشد.")
    return FileResponse(
        path, headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"}
    )


# --------------------------------------------------------------------------
# SMS log
# --------------------------------------------------------------------------
@router.get("/sms")
async def sms_log(request: Request, page: int = 1, per_page: int = 30) -> dict[str, Any]:
    actor = staff(request)
    page = max(1, min(page, 200))
    per_page = max(1, min(per_page, 100))
    rows = repo.select(
        actor,
        "sms_messages",
        order_by="created_at desc",
        limit=per_page,
        offset=(page - 1) * per_page,
    )
    inbound = repo.select(actor, "sms_inbound", order_by="received_at desc", limit=20)
    return {
        "items": [
            {
                k: r[k]
                for k in (
                    "id",
                    "phone",
                    "kind",
                    "body",
                    "status",
                    "attempts",
                    "last_error",
                    "sent_at",
                    "created_at",
                )
            }
            for r in rows
        ],
        "inbound": inbound,
        "total": repo.count(actor, "sms_messages"),
        "page": page,
        "per_page": per_page,
        "provider": __import__("api.config", fromlist=["settings"]).settings.SMS_PROVIDER,
    }


@router.post("/sms/retry")
async def sms_retry(request: Request) -> dict[str, Any]:
    actor = staff(request)
    csrf(request, actor)
    return sms.retry_failed()
