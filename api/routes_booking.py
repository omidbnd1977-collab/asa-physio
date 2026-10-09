"""Patient-facing booking API: register, login, slot grid, reserve."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Body, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import clinical, messages, metrics, patients, ratelimit, repo, scheduling, schemas, sms
from .config import settings
from .errors import AppError, BadRequest, Forbidden, NotFound, PayloadTooLarge
from .jalali import to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM

router = APIRouter(prefix="/api/portal", tags=["portal"])


def _public_mri_url(raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BadRequest("لینک MRI باید یک آدرس معتبر http یا https باشد.", code="bad_mri_url")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise BadRequest("این آدرس برای دریافت MRI مجاز نیست.", code="blocked_mri_url")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BadRequest("آدرس MRI قابل دسترسی نیست.", code="unreachable_mri_url") from exc
    for addr_info in infos:
        address = addr_info[4][0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise BadRequest("دریافت از شبکه داخلی یا آدرس خصوصی مجاز نیست.", code="blocked_mri_url")
    return raw


async def _download_remote_mri(url: str) -> tuple[bytes, str]:
    current = _public_mri_url(url)
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0), follow_redirects=False) as client:
        for _ in range(4):
            async with client.stream("GET", current, headers={"Accept": "image/*"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    if not location:
                        raise BadRequest("لینک MRI قابل دریافت نیست.", code="mri_download_failed")
                    current = _public_mri_url(urljoin(current, location))
                    continue
                if response.status_code != 200:
                    raise BadRequest("لینک MRI قابل دریافت نیست.", code="mri_download_failed")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if not content_type.startswith("image/"):
                    raise BadRequest("لینک MRI باید به یک فایل تصویر اشاره کند.", code="bad_mri_type")
                length = response.headers.get("content-length")
                if length and int(length) > settings.MRI_REMOTE_MAX_BYTES:
                    raise PayloadTooLarge("حجم فایل MRI از سقف فنی مجاز بیشتر است.")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > settings.MRI_REMOTE_MAX_BYTES:
                        raise PayloadTooLarge("حجم فایل MRI از سقف فنی مجاز بیشتر است.")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    raise BadRequest("تعداد redirectهای لینک MRI بیش از حد مجاز است.", code="mri_download_failed")


def client_ip(request: Request) -> str:
    if settings.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "0.0.0.0")[:45]  # noqa: S104


def current_patient(request: Request) -> dict | None:
    return patients.resolve(request.cookies.get(patients.PATIENT_COOKIE))


def _set_session(response: Response, patient_id: int, ip: str) -> None:
    sid, _ = patients.start_session(patient_id, ip)
    response.set_cookie(
        patients.PATIENT_COOKIE,
        sid,
        httponly=True,
        samesite="strict",
        secure=settings.is_prod,
        max_age=settings.PATIENT_SESSION_TTL_H * 3600,
        path="/",
    )


def appt_view(a: dict) -> dict[str, Any]:
    return {
        "public_id": a["public_id"],
        "slot_date": a["slot_date"],
        "slot_time": a["slot_time"],
        "jalali": to_jalali_str(a["slot_date"]),
        "label": to_jalali_long(a["slot_date"]),
        "cabin": a["cabin"],
        "status": a["status"],
        "kind": a["kind"],
        "attendance": a["attendance"],
        "note": a["note"],
    }


# --------------------------------------------------------------------------
# registration  (multipart: the medication photo comes with it)
# --------------------------------------------------------------------------
@router.post("/register", status_code=201)
async def register(
    request: Request,
    response: Response,
    full_name: str = Form(...),
    national_id: str = Form(...),
    birth_date: str = Form(...),
    phone: str = Form(...),
    mri_link: str = Form(""),
    ortho_doctor: str = Form(""),
    website: str = Form(""),
    med_photo: UploadFile | None = File(None),
    mri_file: UploadFile | None = File(None),
) -> JSONResponse:
    ip = client_ip(request)
    ratelimit.enforce(
        ratelimit.ip_bucket("register", ip),
        settings.RL_REGISTER_IP_PER_DAY,
        86400,
        message="تعداد ثبت‌نام از این دستگاه زیاد بوده است. لطفاً تلفنی تماس بگیرید.",
    )

    data = schemas.PatientRegisterIn(
        full_name=full_name,
        national_id=national_id,
        birth_date=birth_date,
        phone=phone,
        mri_link=mri_link,
        ortho_doctor=ortho_doctor,
        website=website,
    )
    if data.website.strip():  # honeypot
        return JSONResponse({"ok": True, "redirect": "/booking/reserve"}, status_code=201)

    ratelimit.enforce(
        ratelimit.id_bucket("register", data.national_id),
        3,
        86400,
        message="برای این کد ملی امروز ثبت‌نام انجام شده است.",
    )

    photo = patients.save_upload(
        med_photo, prefix=data.national_id[-4:], max_bytes=settings.MEDS_UPLOAD_MAX_BYTES
    )
    mri = patients.save_upload(mri_file, prefix=f"mri-{data.national_id[-4:]}")
    mri_warning = ""
    if data.mri_link:
        try:
            remote_blob, _ = await _download_remote_mri(data.mri_link)
            mri = patients.save_bytes(
                remote_blob, prefix=f"mri-{data.national_id[-4:]}", max_bytes=settings.MRI_REMOTE_MAX_BYTES
            )
        except AppError as exc:
            mri_warning = exc.message
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            info("portal.mri_download_failed", error=type(exc).__name__)
            mri_warning = "لینک MRI قابل دریافت نیست؛ ثبت‌نام شما انجام شد و پزشک می‌تواند بعداً فایل را بارگذاری کند."
    patient = patients.register(data.model_dump(), med_photo=photo, mri_file=mri, ip=ip)

    # layer: the SMS the user asked for — "اطلاعات شما ثبت شد"
    _, sent = sms.send_now(
        patient["phone"],
        sms.body_registered(patient["full_name"]),
        "registered",
        patient_id=patient["id"],
    )
    metrics.record("patient_registered", meta_phone_fp=True)
    _set_session(response, patient["id"], ip)
    info("portal.registered", patient_id=patient["id"], sms_sent=sent)
    return JSONResponse(
        {
            "ok": True,
            "sms_sent": sent,
            "patient": patients.public_view(patient),
            "mri_warning": mri_warning,
            "redirect": "/booking/reserve",
        },
        status_code=201,
        headers=dict(response.headers),
    )


# --------------------------------------------------------------------------
# returning patient
# --------------------------------------------------------------------------
@router.post("/login")
async def login(
    request: Request, response: Response, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    ip = client_ip(request)
    ratelimit.enforce(
        ratelimit.ip_bucket("plogin", ip),
        settings.RL_PATIENT_LOGIN_IP_PER_15MIN,
        900,
        message="تلاش‌های ناموفق زیاد بود. چند دقیقه صبر کنید.",
    )
    data = schemas.PatientLoginIn.model_validate(payload)
    ratelimit.enforce(
        ratelimit.id_bucket("plogin", data.national_id),
        8,
        900,
        message="تلاش‌های ناموفق زیاد بود. چند دقیقه صبر کنید.",
    )
    patient = patients.login(data.national_id, data.birth_date)
    _set_session(response, patient["id"], ip)
    return {"ok": True, "patient": patients.public_view(patient), "redirect": "/booking/reserve"}


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> Response:
    sid = request.cookies.get(patients.PATIENT_COOKIE)
    if sid:
        patients.revoke(sid)
    response.delete_cookie(patients.PATIENT_COOKIE, path="/")
    return Response(status_code=204)


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    p = patients.require(current_patient(request))
    rows = repo.select(
        SYSTEM,
        "appointments",
        where="patient_id = ? AND status != 'cancelled'",
        params=[p["id"]],
        order_by="slot_date desc",
    )
    rows.sort(key=lambda a: (a["slot_date"], a["slot_time"]), reverse=True)
    return {
        "patient": patients.public_view(p),
        "appointments": [appt_view(a) for a in rows],
        "unread_messages": messages.unread_for_patient(p["id"]),
    }


# --------------------------------------------------------------------------
# the slot grid
# --------------------------------------------------------------------------
@router.get("/days")
async def days(request: Request) -> dict[str, Any]:
    patients.require(current_patient(request))
    return {
        "days": scheduling.open_days(),
        "slots": scheduling.SLOTS,
        "cabins": scheduling.CABINS,
        "open": f"{scheduling.OPEN_HOUR}:00",
        "close": f"{scheduling.CLOSE_HOUR}:00",
    }


@router.get("/slots")
async def slots(request: Request, response: Response, date: str = "") -> Any:
    patients.require(current_patient(request))
    if not date:
        raise BadRequest("تاریخ مشخص نشده است.")
    try:
        dt.date.fromisoformat(date)
    except ValueError as exc:
        raise BadRequest("تاریخ معتبر نیست.") from exc
    grid = scheduling.day_grid(date)
    # live availability must not be cached by the browser
    response.headers["Cache-Control"] = "no-store"
    return grid


@router.post("/appointments", status_code=201)
async def book(request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    p = patients.require(current_patient(request))
    ratelimit.enforce(
        ratelimit.id_bucket("appt", str(p["id"])),
        settings.RL_APPT_PATIENT_PER_DAY,
        86400,
        message="تعداد نوبت‌های امروز شما به حد مجاز رسیده است. لطفاً تماس بگیرید.",
    )
    data = schemas.AppointmentIn.model_validate(payload)
    appt = scheduling.allocate(
        p["id"],
        data.slot_date,
        data.slot_time,
        kind="first",
        booked_by="patient",
        note=data.note,
    )
    when = scheduling.describe(appt)

    # Coming back after a long break: thank them first, so the warm message is the
    # one they read before the booking confirmation.
    gap = clinical.returning_after_gap(p["id"], data.slot_date)
    welcomed = False
    if gap:
        _, welcomed = sms.send_now(
            p["phone"],
            sms.body_welcome_back(p["full_name"], when, gap),
            "welcome_back",
            patient_id=p["id"],
            appointment_id=appt["id"],
        )
        info("portal.welcome_back", patient_id=p["id"], gap_days=gap)

    _, sent = sms.send_now(
        p["phone"],
        sms.body_booked(p["full_name"], when, appt["public_id"]),
        "booked",
        patient_id=p["id"],
        appointment_id=appt["id"],
    )
    metrics.record("appointment_booked")
    messages.system_note(
        p["id"],
        f"نوبت جدید ثبت شد: {when} — کد پیگیری {appt['public_id']}",
        kind="booked",
        appointment_id=appt["id"],
    )
    from . import cache

    cache.purge("admin:appointments")
    info("portal.booked", appointment_id=appt["id"], sms_sent=sent)
    return {
        "ok": True,
        "sms_sent": sent,
        "welcomed_back": welcomed,
        "gap_days": gap,
        "code": appt["public_id"],
        "when": when,
        "cabin": appt["cabin"],
        "redirect": f"/booking/done?code={appt['public_id']}",
    }


@router.post("/appointments/{public_id}/cancel")
async def cancel(public_id: str, request: Request) -> dict[str, Any]:
    p = patients.require(current_patient(request))
    rows = repo.select(
        SYSTEM,
        "appointments",
        where="public_id = ? AND patient_id = ?",
        params=[public_id[:10], p["id"]],
        limit=1,
    )
    if not rows:
        raise NotFound("این نوبت پیدا نشد.")
    appt = rows[0]
    when = scheduling.slot_datetime(appt["slot_date"], appt["slot_time"])
    from .jalali import now_tehran

    if when - now_tehran() < dt.timedelta(hours=3):
        raise Forbidden("لغو نوبت تا ۳ ساعت مانده به زمان مراجعه ممکن نیست. تماس بگیرید.")
    repo.update(
        SYSTEM, "appointments", {"status": "cancelled"}, where="id = ?", params=[appt["id"]]
    )
    # attributed to the patient, so the doctor-panel inbox badge lights up
    messages.system_note(
        p["id"],
        f"نوبت {scheduling.describe(appt)} توسط بیمار لغو شد.",
        kind="cancelled",
        appointment_id=appt["id"],
        from_patient=True,
    )
    from . import cache

    cache.purge("admin:appointments")
    return {"ok": True}


# --------------------------------------------------------------------------
# doctor <-> patient messaging  (the in-app channel between the two panels)
# --------------------------------------------------------------------------
@router.get("/messages")
async def my_messages(request: Request) -> dict[str, Any]:
    """The patient's own thread. Fetching it marks the clinic's messages as read."""
    p = patients.require(current_patient(request))
    out = messages.thread(p["id"])
    messages.mark_read_by_patient(p["id"])
    return {"messages": out, "unread": 0}


@router.post("/messages", status_code=201)
async def send_message(request: Request, payload: dict[str, Any] = Body(default={})) -> Any:
    p = patients.require(current_patient(request))
    ratelimit.enforce(
        ratelimit.id_bucket("msg", str(p["id"])),
        30,
        86400,
        message="تعداد پیام‌های امروز شما به حد مجاز رسیده است.",
    )
    data = schemas.MessageIn.model_validate(payload)
    saved = messages.post(p["id"], "patient", data.body)
    return {"ok": True, **saved}


@router.get("/history")
async def my_history(request: Request) -> dict[str, Any]:
    """The treatment history the doctor recorded — findings stay with the clinic;
    the patient sees what was done and what the plan for next time is."""
    p = patients.require(current_patient(request))
    hist = clinical.history(SYSTEM, p["id"], limit=50)
    return {
        "history": [
            {
                "label": h["label"],
                "jalali": h["jalali"],
                "treatments": h["treatments"],
                "plan": h["plan"],
                "pain_before": h["pain_before"],
                "pain_after": h["pain_after"],
            }
            for h in hist
        ]
    }
