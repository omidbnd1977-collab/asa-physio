"""Doctor/staff API for the patient portal: patients, appointments, reschedule,
next session, attendance, and the SMS log."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, File, Request, Response, UploadFile
from fastapi.responses import FileResponse

from . import (
    audit,
    auth,
    cache,
    clinical,
    messages,
    metrics,
    ownership,
    patients,
    repo,
    scheduling,
    schemas,
    sms,
    staff_users,
)
from . import (
    permissions as perms,
)
from .errors import BadRequest, Conflict, Forbidden, NotFound
from .jalali import now_tehran, to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM, Actor

router = APIRouter(prefix="/api/admin", tags=["doctor"])


def staff(request: Request) -> Actor:
    return auth.require(auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE)), "staff")


def owner(request: Request) -> Actor:
    return auth.require(auth.resolve_session(request.cookies.get(auth.SESSION_COOKIE)), "owner")


def csrf(request: Request, actor: Actor) -> None:
    auth.check_csrf(actor.session_id or "", request.headers.get("x-csrf-token"))


def need(actor: Actor, permission: str) -> Actor:
    return auth.require_perm(actor, permission)


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
        "booked_by_user_id": a.get("booked_by_user_id"),
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
    ow, oparams = ownership.owned_patients_where(actor)
    marks = ",".join("?" for _ in ids)
    rows = repo.select(
        SYSTEM,
        "patients",
        where=f"id IN ({marks}) AND ({ow})",
        params=[*ids, *oparams],
    )
    return {r["id"]: r for r in rows}


def _owned_appts_clause(actor: Actor) -> tuple[str, list[Any]]:
    ow, params = ownership.owned_patients_where(actor)
    # ow is a fixed fragment from ownership.owned_patients_where (never user input).
    return f"patient_id IN (SELECT id FROM patients WHERE {ow})", list(params)  # noqa: S608


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
    actor = need(staff(request), perms.APPOINTMENTS_VIEW)
    page = max(1, min(page, 200))
    per_page = max(1, min(per_page, 200))
    today = now_tehran().date().isoformat()

    if date:
        where, params = "slot_date = ? AND status = 'booked'", [date]
    elif scope == "today":
        where, params = "slot_date = ? AND status = 'booked'", [today]
    elif scope == "past":
        where, params = "slot_date < ?", [today]
    elif scope == "all":
        where, params = "1=1", []
    else:
        where, params = "slot_date >= ? AND status = 'booked'", [today]

    own_sql, own_params = _owned_appts_clause(actor)
    where = f"({where}) AND ({own_sql})"
    params = [*params, *own_params]
    scope_key = ownership.clinic_owner_id(actor) or 0
    key = f"admin:appointments:{scope_key}:{where}:{params}:{page}:{per_page}"

    def build() -> dict[str, Any]:
        rows = repo.select(
            SYSTEM,
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
            "total": repo.count(SYSTEM, "appointments", where=where, params=params),
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
    actor = need(staff(request), perms.APPOINTMENTS_VIEW)
    d = date or now_tehran().date().isoformat()
    g = scheduling.day_grid(d, for_patient=False)
    own_sql, own_params = _owned_appts_clause(actor)
    rows = repo.select(
        SYSTEM,
        "appointments",
        where=f"slot_date = ? AND status != 'cancelled' AND ({own_sql})",
        params=[d, *own_params],
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


# --------------------------------------------------------------------------
# clinic closure dates
# --------------------------------------------------------------------------
@router.get("/closed-days")
async def list_closed_days(request: Request) -> dict[str, Any]:
    owner(request)
    days = scheduling.open_days()
    labels = {day["date"]: day["label"] for day in days}
    today = now_tehran().date().isoformat()
    closed = scheduling.closed_days_map()
    return {
        "days": days,
        "closed_days": [
            {
                "slot_date": date,
                "reason": reason,
                "label": labels.get(date, to_jalali_long(date)),
            }
            for date, reason in sorted(closed.items())
            if date >= today
        ],
    }


@router.post("/closed-days", status_code=201)
async def close_clinic_day(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    slot_date = payload.get("date", "")
    reason = payload.get("reason", "")
    if not isinstance(slot_date, str) or not isinstance(reason, str):
        raise BadRequest("تاریخ یا توضیح تعطیلی معتبر نیست.")
    reason = reason.strip()
    if len(reason) > 200:
        raise BadRequest("توضیح تعطیلی نباید بیشتر از ۲۰۰ نویسه باشد.")
    try:
        dt.date.fromisoformat(slot_date)
    except ValueError as exc:
        raise BadRequest("تاریخ تعطیلی معتبر نیست.") from exc
    available_dates = {day["date"] for day in scheduling.open_days()}
    if slot_date not in available_dates:
        raise BadRequest("فقط می‌توانید روزهای بازهٔ نوبت‌دهی را قفل کنید.")
    with repo.tx():
        if repo.count(
            actor,
            "appointments",
            where="slot_date = ? AND status = 'booked'",
            params=[slot_date],
        ):
            raise Conflict(
                "برای این روز نوبت ثبت شده است؛ ابتدا نوبت‌های بیماران را تعیین تکلیف کنید.",
                code="day_has_bookings",
            )
        if slot_date in scheduling.closed_days_map():
            raise Conflict("این روز از قبل قفل شده است.", code="day_already_closed")
        audit.record(
            actor,
            "clinic.day_closed",
            "clinic_closed_day",
            slot_date,
            after={"date": slot_date, "reason": reason},
        )
    info("doctor.closed_day", date=slot_date)
    return {"date": slot_date, "label": to_jalali_long(slot_date), "reason": reason}


@router.delete("/closed-days/{slot_date}")
async def reopen_clinic_day(slot_date: str, request: Request) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    try:
        dt.date.fromisoformat(slot_date)
    except ValueError as exc:
        raise BadRequest("تاریخ تعطیلی معتبر نیست.") from exc
    with repo.tx():
        closed = scheduling.closed_days_map()
        if slot_date not in closed:
            raise NotFound("این روز قفل نشده است.")
        audit.record(
            actor,
            "clinic.day_reopened",
            "clinic_closed_day",
            slot_date,
            before={"date": slot_date, "reason": closed[slot_date]},
        )
    info("doctor.reopened_day", date=slot_date)
    return {"date": slot_date, "reopened": True}


@router.patch("/appointments/{public_id}/move")
async def move(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Reschedule. The patient is told by SMS."""
    actor = need(staff(request), perms.APPOINTMENTS_MOVE)
    csrf(request, actor)
    data = schemas.AppointmentMoveIn.model_validate(payload)

    old = ownership.get_owned_appointment(actor, public_id)
    if old["status"] == "cancelled":
        raise Conflict("این نوبت لغو شده است.")
    if old["slot_date"] == data.slot_date and old["slot_time"] == data.slot_time:
        raise BadRequest("زمان جدید با زمان فعلی یکی است.")

    p = repo.select(SYSTEM, "patients", where="id = ?", params=[old["patient_id"]], limit=1)
    if not p:
        raise NotFound("بیمار پیدا نشد.")
    patient = p[0]
    ownership.assert_patient_row_owned(actor, patient)

    old_when = scheduling.describe(old)
    original_booked_by = old.get("booked_by") or "staff"
    original_booked_by_user_id = old.get("booked_by_user_id")
    # cancel + allocate share one BEGIN IMMEDIATE; booker identity preserved in reschedule
    new = scheduling.reschedule(old, data.slot_date, data.slot_time)

    new_when = scheduling.describe(new)
    messages.system_note(
        patient["id"],
        f"نوبت شما تغییر کرد. زمان قبلی: {old_when} — زمان جدید: {new_when}",
        kind="rescheduled",
        appointment_id=new["id"],
    )
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
    moved_at = _now()
    audit.record(
        actor,
        "appointment.moved",
        "appointment",
        new["public_id"],
        before={
            "public_id": old["public_id"],
            "when": old_when,
            "booked_by": original_booked_by,
            "booked_by_user_id": original_booked_by_user_id,
        },
        after={
            "public_id": new["public_id"],
            "when": new_when,
            "cabin": new["cabin"],
            "booked_by": original_booked_by,
            "booked_by_user_id": original_booked_by_user_id,
            "moved_by_user_id": actor.user_id,
            "moved_by_role": actor.role,
            "moved_at": moved_at,
        },
    )
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
    actor = need(staff(request), perms.APPOINTMENTS_CREATE)
    csrf(request, actor)
    data = schemas.FollowUpIn.model_validate(payload)
    patient = ownership.get_owned_patient(actor, public_id)
    appt = scheduling.allocate(
        patient["id"],
        data.slot_date,
        data.slot_time,
        kind="followup",
        booked_by="staff",
        booked_by_user_id=actor.user_id,
        note=data.note,
    )
    when = scheduling.describe(appt)
    gap = clinical.returning_after_gap(patient["id"], data.slot_date)
    messages.system_note(
        patient["id"],
        f"جلسه‌ی بعدی شما ثبت شد: {when}" + (f" — {data.note}" if data.note else ""),
        kind="followup",
        appointment_id=appt["id"],
    )
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
    from . import notify

    # Manager alert is independent of patient-notify toggle; failures never undo the booking.
    notify.alert_new_appointment(
        name=patient["full_name"],
        phone=patient["phone"],
        when=when,
        code=appt["public_id"],
        appointment_id=appt["id"],
    )
    audit.record(
        actor,
        "appointment.followup",
        "appointment",
        appt["public_id"],
        after={
            "patient_public_id": patient["public_id"],
            "when": when,
            "cabin": appt["cabin"],
            "booked_by": "staff",
            "booked_by_user_id": actor.user_id,
            "role": actor.role,
            "at": _now(),
        },
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
    if data.status == "cancelled":
        need(actor, perms.APPOINTMENTS_CANCEL)
    else:
        # Attendance / clinical status changes stay with the clinic owner.
        auth.require(actor, "owner")
    appt = ownership.get_owned_appointment(actor, public_id)
    repo.update(
        SYSTEM,
        "appointments",
        {"status": data.status, "updated_at": _now()},
        where="id = ?",
        params=[appt["id"]],
    )
    audit.record(
        actor,
        "appointment.status_changed",
        "appointment",
        appt["public_id"],
        before={"status": appt["status"]},
        after={"status": data.status},
    )
    if data.status == "cancelled":
        messages.system_note(
            appt["patient_id"],
            f"نوبت {scheduling.describe(appt)} توسط کلینیک لغو شد."
            " برای زمان جدید پیام بدهید یا تماس بگیرید.",
            kind="cancelled",
            appointment_id=appt["id"],
        )
    elif data.status == "no_show":
        messages.system_note(
            appt["patient_id"],
            f"نوبت {scheduling.describe(appt)} بدون مراجعه ثبت شد."
            " برای تعیین وقت جدید در خدمت شما هستیم.",
            kind="cancelled",
            appointment_id=appt["id"],
        )
    sent = False
    if data.status == "cancelled" and data.notify:
        p = repo.select(SYSTEM, "patients", where="id = ?", params=[appt["patient_id"]], limit=1)
        if p:
            ownership.assert_patient_row_owned(actor, p[0])
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
    actor = owner(request)
    csrf(request, actor)
    appt = ownership.get_owned_appointment(actor, public_id)
    p = repo.select(SYSTEM, "patients", where="id = ?", params=[appt["patient_id"]], limit=1)
    if not p:
        raise NotFound("بیمار پیدا نشد.")
    ownership.assert_patient_row_owned(actor, p[0])
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
    actor = need(staff(request), perms.PATIENTS_VIEW)
    page = max(1, min(page, 500))
    per_page = max(1, min(per_page, 100))
    ow, oparams = ownership.owned_patients_where(actor)
    if q:
        term = f"%{q.strip()[:40]}%"
        where = f"({ow}) AND (full_name LIKE ? OR phone LIKE ? OR national_id LIKE ?)"
        params: list[Any] = [*oparams, term, term, term]
    else:
        where, params = ow, list(oparams)
    rows = repo.select(
        SYSTEM,
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
            SYSTEM,
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
        "total": repo.count(SYSTEM, "patients", where=where, params=params),
    }


@router.patch("/patients/{public_id}/note")
async def patient_note(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = need(staff(request), perms.PATIENTS_EDIT)
    csrf(request, actor)
    data = schemas.PatientNoteIn.model_validate(payload)
    patient = ownership.get_owned_patient(actor, public_id)
    repo.update(
        SYSTEM,
        "patients",
        {"staff_note": data.staff_note, "updated_at": _now()},
        where="id = ?",
        params=[patient["id"]],
    )
    return {"ok": True}


@router.patch("/patients/{public_id}")
async def patient_update(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Staff correction of a patient record. Audited, because it overwrites data the
    patient supplied themselves and the two must stay distinguishable."""
    actor = need(staff(request), perms.PATIENTS_EDIT)
    csrf(request, actor)
    data = schemas.PatientUpdateIn.model_validate(payload)
    patient = dict(ownership.get_owned_patient(actor, public_id))
    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if not changes:
        return {"ok": True, "changed": []}
    before = {k: patient[k] for k in changes if k in patient}
    repo.update(
        SYSTEM,
        "patients",
        {**changes, "updated_at": _now()},
        where="id = ?",
        params=[patient["id"]],
    )
    audit.record(
        actor, "patient.updated", "patient", patient["public_id"], before=before, after=changes
    )
    return {"ok": True, "changed": sorted(changes)}


@router.get("/patients/{public_id}/audit")
async def patient_audit(public_id: str, request: Request) -> dict[str, Any]:
    actor = need(staff(request), perms.PATIENTS_VIEW)
    patient = ownership.get_owned_patient(actor, public_id)
    return {"items": audit.list_for("patient", patient["public_id"])}


@router.get("/appointments/{public_id}/audit")
async def appointment_audit(public_id: str, request: Request) -> dict[str, Any]:
    actor = need(staff(request), perms.APPOINTMENTS_VIEW)
    appt = ownership.get_owned_appointment(actor, public_id)
    return {"items": audit.list_for("appointment", appt["public_id"])}


@router.get("/patients/{public_id}/photo")
async def patient_photo(public_id: str, request: Request) -> Response:
    """Medication photo. Staff only — uploads are never served from a public path."""
    actor = need(staff(request), perms.PATIENTS_VIEW)
    patient = ownership.get_owned_patient(actor, public_id)
    if not patient["med_photo"]:
        raise NotFound("فایلی ثبت نشده است.")
    path = patients.upload_path(patient["med_photo"])
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
    actor = owner(request)
    return cache.memo("admin:catalog", 30, lambda: clinical.catalog(actor))


@router.post("/catalog/body-parts", status_code=201)
async def add_body_part(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    data = schemas.CatalogItemIn.model_validate(payload)
    out = clinical.add_catalog_item(actor, "body_parts", data.name, data.category)
    cache.purge("admin:catalog")
    return out


@router.post("/catalog/treatments", status_code=201)
async def add_treatment(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
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
    actor = owner(request)
    patient = ownership.get_owned_patient(actor, public_id)
    pid = patient["id"]
    return {"history": clinical.history(actor, pid), "summary": clinical.summary(actor, pid)}


@router.post("/patients/{public_id}/sessions", status_code=201)
async def record_treatment(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """The doctor records what was done: affected area, treatment performed, notes."""
    actor = owner(request)
    csrf(request, actor)
    data = schemas.TreatmentSessionIn.model_validate(payload)

    patient = ownership.get_owned_patient(actor, public_id)

    appt = None
    if data.appointment_id:
        a = repo.select(
            SYSTEM,
            "appointments",
            where=(
                "public_id = ? AND patient_id = ? AND patient_id IN "
                "(SELECT id FROM patients WHERE owner_id = ?)"
            ),
            params=[data.appointment_id, patient["id"], actor.user_id],
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
            SYSTEM,
            "appointments",
            {"status": "attended", "updated_at": _now()},
            where="id = ?",
            params=[appt["id"]],
        )
        metrics.record("session_attended")

    names = [t["name"] for t in clinical._labels(actor, "treatments", data.treatment_ids)]
    note = f"خلاصه‌ی جلسه‌ی {to_jalali_long(out['session_date'])} ثبت شد: {'، '.join(names)}"
    if data.plan:
        note += f" — برنامه‌ی جلسه‌ی بعد: {data.plan}"
    messages.system_note(
        patient["id"], note, kind="session", appointment_id=appt["id"] if appt else None
    )

    sent = False
    if data.notify:
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
    actor = need(staff(request), perms.PATIENTS_EDIT)
    csrf(request, actor)
    patient = ownership.get_owned_patient(actor, public_id)
    name = patients.save_upload(mri_file, prefix=f"mri-{public_id[:6]}")
    if not name:
        raise BadRequest("فایلی دریافت نشد.")
    repo.update(
        SYSTEM,
        "patients",
        {"mri_file": name, "updated_at": _now()},
        where="id = ?",
        params=[patient["id"]],
    )
    return {"ok": True, "file": name}


@router.get("/patients/{public_id}/mri")
async def view_mri(public_id: str, request: Request) -> Response:
    actor = need(staff(request), perms.PATIENTS_VIEW)
    patient = ownership.get_owned_patient(actor, public_id)
    if not patient["mri_file"]:
        raise NotFound("فایل MRI ثبت نشده است.")
    path = patients.upload_path(patient["mri_file"])
    if path is None:
        raise NotFound("فایل پیدا نشد.")
    return FileResponse(
        path, headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"}
    )


# --------------------------------------------------------------------------
# SMS log
# --------------------------------------------------------------------------
def _can_open_sms_panel(actor: Actor) -> bool:
    return (
        actor.role == "owner"
        or actor.can(perms.RECEPTION_QUEUE)
        or actor.can(perms.MESSAGES_VIEW)
    )


@router.get("/sms")
async def sms_log(request: Request, page: int = 1, per_page: int = 30) -> dict[str, Any]:
    actor = staff(request)
    if not _can_open_sms_panel(actor):
        raise Forbidden("اجازهٔ دسترسی به این بخش را ندارید.")
    page = max(1, min(page, 200))
    per_page = max(1, min(per_page, 100))
    items: list[dict[str, Any]] = []
    total = 0
    # Outbound log stays owner-only (clinic-wide operational view).
    if actor.role == "owner":
        ow, oparams = ownership.owned_patients_where(actor)
        sms_where = (
            f"patient_id IS NULL OR patient_id IN (SELECT id FROM patients WHERE {ow})"  # noqa: S608
        )
        rows = repo.select(
            SYSTEM,
            "sms_messages",
            where=sms_where,
            params=oparams,
            order_by="created_at desc",
            limit=per_page,
            offset=(page - 1) * per_page,
        )
        total = repo.count(SYSTEM, "sms_messages", where=sms_where, params=oparams)
        items = [
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
        ]
    iw, iparams = ownership.inbound_visible_where(actor)
    inbound = repo.select(
        SYSTEM,
        "sms_inbound",
        where=iw,
        params=iparams,
        order_by="received_at desc",
        limit=50,
    )
    return {
        "items": items,
        "inbound": [dict(r) for r in inbound],
        "total": total,
        "page": page,
        "per_page": per_page,
        "can_manage_queue": ownership.can_manage_reception_queue(actor),
        "provider": __import__("api.config", fromlist=["settings"]).settings.SMS_PROVIDER,
    }


@router.post("/sms/inbound/{inbound_id}/assign")
async def assign_inbound_sms(
    inbound_id: int, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Link an unassigned inbound SMS to an owned patient (reception queue)."""
    actor = staff(request)
    if not ownership.can_manage_reception_queue(actor):
        raise Forbidden("اجازهٔ مدیریت صف پذیرش را ندارید.")
    csrf(request, actor)
    data = schemas.InboundAssignIn.model_validate(payload)
    patient = ownership.get_owned_patient(actor, data.patient_public_id)
    rows = repo.select(
        SYSTEM,
        "sms_inbound",
        where="id = ? AND owner_id IS NULL",
        params=[inbound_id],
        limit=1,
    )
    if not rows:
        raise NotFound("پیامک بدون مالک پیدا نشد.")
    before = dict(rows[0])
    n = repo.update(
        SYSTEM,
        "sms_inbound",
        {"patient_id": patient["id"], "owner_id": patient["owner_id"]},
        where="id = ? AND owner_id IS NULL",
        params=[inbound_id],
    )
    if n != 1:
        raise Conflict("این پیامک هم‌اکنون تخصیص یافته است.")
    audit.record(
        actor,
        "sms_inbound.assigned",
        "sms_inbound",
        inbound_id,
        before={"owner_id": None, "patient_id": before.get("patient_id")},
        after={
            "owner_id": patient["owner_id"],
            "patient_id": patient["id"],
            "patient_public_id": patient["public_id"],
        },
    )
    return {
        "ok": True,
        "id": inbound_id,
        "patient_public_id": patient["public_id"],
        "owner_id": patient["owner_id"],
    }


@router.post("/sms/retry")
async def sms_retry(request: Request) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    return sms.retry_failed()


# --------------------------------------------------------------------------
# doctor <-> patient messaging
# --------------------------------------------------------------------------
@router.get("/messages")
async def message_inbox(request: Request) -> dict[str, Any]:
    """One row per patient: latest message + unread count, newest first."""
    actor = need(staff(request), perms.MESSAGES_VIEW)
    return {"items": messages.inbox(actor), "unread": messages.unread_for_staff(actor)}


@router.get("/messages/unread")
async def message_unread(request: Request) -> dict[str, int]:
    """Lightweight badge endpoint the panel polls."""
    actor = need(staff(request), perms.MESSAGES_VIEW)
    return {"unread": messages.unread_for_staff(actor)}


@router.get("/patients/{public_id}/messages")
async def patient_thread(public_id: str, request: Request) -> dict[str, Any]:
    """The full thread with one patient. Fetching it marks their messages as read."""
    actor = need(staff(request), perms.MESSAGES_VIEW)
    patient = ownership.get_owned_patient(actor, public_id)
    out = messages.thread(patient["id"])
    messages.mark_read_by_staff(patient["id"])
    return {
        "patient": {"public_id": patient["public_id"], "full_name": patient["full_name"]},
        "messages": out,
        "unread": messages.unread_for_staff(actor),
    }


@router.post("/patients/{public_id}/messages", status_code=201)
async def reply_to_patient(
    public_id: str, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """The doctor writes back. The patient sees it in the portal; optionally it is
    also sent by SMS so it reaches them even if they never open the portal."""
    actor = need(staff(request), perms.MESSAGES_VIEW)
    csrf(request, actor)
    data = schemas.MessageIn.model_validate(payload)
    patient = ownership.get_owned_patient(actor, public_id)

    saved = messages.post(patient["id"], "staff", data.body, created_by=actor.user_id)
    sent = False
    if data.notify:
        _, sent = sms.send_now(
            patient["phone"],
            sms.body_custom(patient["full_name"], saved["body"]),
            "custom",
            patient_id=patient["id"],
        )
    info("doctor.replied", patient_id=patient["id"], sms_sent=sent)
    return {"ok": True, "sms_sent": sent, **saved}


# --------------------------------------------------------------------------
# staff patient create (secretary with patients.create)
# --------------------------------------------------------------------------
@router.post("/patients", status_code=201)
async def create_patient(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Register a patient from the staff panel. No patient session is opened."""
    actor = need(staff(request), perms.PATIENTS_CREATE)
    csrf(request, actor)
    oid = ownership.clinic_owner_id(actor)
    if oid is None:
        raise Forbidden("حساب شما به هیچ کلینیکی متصل نیست.")
    data = schemas.PatientRegisterIn.model_validate({**payload, "website": ""})
    ip = request.client.host if request.client else ""
    patient = patients.register(data.model_dump(), med_photo="", ip=ip, owner_id=oid)
    audit.record(
        actor,
        "patient.created_by_staff",
        "patient",
        patient["public_id"],
        after={
            "full_name": patient["full_name"],
            "phone": patient["phone"],
            "owner_id": oid,
        },
    )
    return {
        "ok": True,
        "public_id": patient["public_id"],
        "full_name": patient["full_name"],
        "phone": patient["phone"],
    }


# --------------------------------------------------------------------------
# secretary management (owner only)
# --------------------------------------------------------------------------
@router.get("/secretaries")
async def list_secretaries(request: Request) -> dict[str, Any]:
    actor = owner(request)
    return {
        "items": staff_users.list_secretaries(actor),
        "permission_catalog": [
            {"id": p, "label": perms.LABELS_FA[p]} for p in perms.ALL
        ],
    }


@router.post("/secretaries", status_code=201)
async def create_secretary(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    data = schemas.SecretaryCreateIn.model_validate(payload)
    return staff_users.create_secretary(
        actor,
        full_name=data.full_name,
        username=data.username,
        password=data.password,
        permissions=data.permissions,
    )


@router.patch("/secretaries/{secretary_id}")
async def update_secretary(
    secretary_id: int, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    data = schemas.SecretaryUpdateIn.model_validate(payload)
    return staff_users.update_secretary(
        actor,
        secretary_id,
        full_name=data.full_name,
        is_active=data.is_active,
        permissions=data.permissions,
    )


@router.post("/secretaries/{secretary_id}/password")
async def reset_secretary_password(
    secretary_id: int, request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    data = schemas.SecretaryPasswordIn.model_validate(payload)
    return staff_users.set_secretary_password(actor, secretary_id, data.password)


@router.get("/orphan-secretaries")
async def orphan_secretaries(request: Request) -> dict[str, Any]:
    actor = owner(request)
    return {"items": staff_users.list_orphan_secretaries(actor)}


@router.post("/orphan-secretaries/{secretary_id}/assign")
async def assign_orphan_secretary(
    secretary_id: int, request: Request
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    return staff_users.assign_orphan_secretary(actor, secretary_id)


@router.get("/orphan-patients")
async def orphan_patients(request: Request) -> dict[str, Any]:
    actor = owner(request)
    return staff_users.list_orphan_patients(actor)


@router.post("/orphan-patients/assign")
async def assign_orphan_patients(
    request: Request, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    actor = owner(request)
    csrf(request, actor)
    data = schemas.OrphanPatientsAssignIn.model_validate(payload)
    return staff_users.assign_orphan_patients(actor, data.public_ids)
