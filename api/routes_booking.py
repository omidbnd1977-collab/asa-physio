"""Patient-facing booking API: register, login, slot grid, reserve."""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpcore
import httpx
from fastapi import APIRouter, Body, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import clinical, messages, metrics, patients, ratelimit, repo, scheduling, schemas, sms
from .config import settings
from .errors import AppError, BadRequest, Conflict, Forbidden, NotFound, PayloadTooLarge
from .jalali import to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM

router = APIRouter(prefix="/api/portal", tags=["portal"])

_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_BLOCKED_MRI_HOSTS = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when the address must not be contacted for an MRI fetch (SSRF)."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.version == 4 and ip in _CGNAT_V4:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


class _PinnedIPBackend(httpcore.AsyncNetworkBackend):
    """Connect TCP to a pre-validated IP while httpx keeps the URL hostname for Host/SNI/TLS."""

    def __init__(self, pinned_ip: str) -> None:
        self._pinned_ip = pinned_ip
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(  # type: ignore[override]
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # `host` is the original hostname (needed upstream for SNI); dial the pinned IP only.
        return await self._inner.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(  # type: ignore[override]
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise RuntimeError("unix sockets are not used for MRI downloads")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _pinned_transport(pinned_ip: str) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport()
    # httpx 0.28 / httpcore 1.x: swap the pool backend so TCP uses the pinned IP.
    transport._pool._network_backend = _PinnedIPBackend(pinned_ip)  # noqa: SLF001
    return transport


def _pin_public_mri_url(raw: str) -> tuple[str, str, str]:
    """Validate URL, resolve DNS, require every answer to be public, return (url, host, pinned_ip)."""
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BadRequest("لینک MRI باید یک آدرس معتبر http یا https باشد.", code="bad_mri_url")
    host = parsed.hostname.rstrip(".").lower()
    if host in _BLOCKED_MRI_HOSTS:
        raise BadRequest("این آدرس برای دریافت MRI مجاز نیست.", code="blocked_mri_url")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BadRequest("آدرس MRI قابل دسترسی نیست.", code="unreachable_mri_url") from exc

    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for addr_info in infos:
        address = addr_info[4][0]
        if address in seen:
            continue
        seen.add(address)
        resolved.append(ipaddress.ip_address(address))

    if not resolved:
        raise BadRequest("آدرس MRI قابل دسترسی نیست.", code="unreachable_mri_url")
    if any(_blocked_ip(ip) for ip in resolved):
        raise BadRequest("دریافت از شبکه داخلی یا آدرس خصوصی مجاز نیست.", code="blocked_mri_url")

    # Prefer IPv4 when both families are public; pin exactly one validated address.
    resolved.sort(key=lambda ip: (ip.version != 4, str(ip)))
    return raw, host, str(resolved[0])


def _public_mri_url(raw: str) -> str:
    """Back-compat validator used by tests; delegates to the pinning resolver."""
    url, _host, _ip = _pin_public_mri_url(raw)
    return url


async def _download_remote_mri(url: str) -> tuple[bytes, str]:
    current = url
    for _ in range(4):
        current, _hostname, pinned_ip = _pin_public_mri_url(current)
        transport = _pinned_transport(pinned_ip)
        async with (
            httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
            ) as client,
            client.stream("GET", current, headers={"Accept": "image/*"}) as response,
        ):
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                if not location:
                    raise BadRequest("لینک MRI قابل دریافت نیست.", code="mri_download_failed")
                current = urljoin(current, location)
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
    consent_accepted: str = Form(""),
    consent_version: str = Form(""),
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
        consent_accepted=consent_accepted,
        consent_version=consent_version,
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
                remote_blob,
                prefix=f"mri-{data.national_id[-4:]}",
                max_bytes=settings.MRI_REMOTE_MAX_BYTES,
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
    if p.get("owner_id") is None:
        raise Conflict(
            "حساب شما هنوز به کلینیک متصل نیست؛ رزرو اینترنتی ممکن نیست. لطفاً تماس بگیرید.",
            code="patient_unassigned",
        )
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
    from . import notify

    notify.alert_new_appointment(
        name=p["full_name"],
        phone=p["phone"],
        when=when,
        code=appt["public_id"],
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
    if appt["status"] != "booked":
        raise Conflict("این نوبت قابل لغو نیست.")
    when = scheduling.slot_datetime(appt["slot_date"], appt["slot_time"])
    from .jalali import now_tehran

    if when - now_tehran() < dt.timedelta(hours=3):
        raise Forbidden("لغو نوبت تا ۳ ساعت مانده به زمان مراجعه ممکن نیست. تماس بگیرید.")
    repo.update(
        SYSTEM,
        "appointments",
        {
            "status": "cancelled",
            "updated_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        where="id = ?",
        params=[appt["id"]],
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


# --------------------------------------------------------------------------
# MRI viewer — patient-only access; file bytes are never publicly exposed.
# --------------------------------------------------------------------------
@router.get("/mri/viewer")
async def mri_viewer(request: Request) -> Response:
    p = patients.require(current_patient(request))
    if not p.get("mri_file") or not patients.upload_path(p["mri_file"]):
        html = """<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>نمایش MRI | آسا فیزیو</title>
<body style="margin:0;background:#080b10;color:#f4f6f8;font:16px sans-serif;padding:32px;line-height:2">
<h1>هنوز فایل MRI در پرونده ثبت نشده است</h1><p>هنگام ثبت‌نام می‌توانید فایل DICOM یا ZIP حاوی تصاویر MRI را ارسال کنید.</p>
<a style="color:#8ee7df" href="/booking/reserve">بازگشت به پنل بیمار</a></body></html>"""
        return Response(
            content=html,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )
    html = r"""<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>نمایش MRI | آسا فیزیو</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#080a0d;color:#f2f4f7;font:15px system-ui,sans-serif}
header{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:14px 18px;background:#11151b;border-bottom:1px solid #29313c}
h1{font-size:1rem;margin:0}a{color:#8be4dc;text-decoration:none}.tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 0}
button,input{font:inherit}button{background:#202833;border:1px solid #3b4654;color:#fff;padding:8px 12px;border-radius:8px;cursor:pointer}
button:disabled{opacity:.4}input[type=range]{width:110px;accent-color:#72d8d0}
main{max-width:1200px;margin:auto;padding:14px}.viewer{background:#000;border:1px solid #303844;border-radius:12px;min-height:55vh;display:grid;place-items:center;overflow:auto}
canvas{max-width:100%;max-height:68vh;object-fit:contain;cursor:grab;touch-action:pan-x pan-y}.status{padding:22px;text-align:center;color:#aab4c2;line-height:1.9}
.meta{display:flex;gap:12px;flex-wrap:wrap;color:#aab4c2;font-size:.82rem;padding:12px 2px}.bar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:12px 0}
.bar input{flex:1;min-width:120px}.notice{font-size:.78rem;color:#aab4c2;line-height:1.8}
@media(max-width:600px){header{align-items:flex-start}.viewer{min-height:48vh}.tools{width:100%}}
</style>
<script src="https://cdn.jsdelivr.net/npm/dicom-parser@1.8.21/dist/dicomParser.min.js" integrity="sha384-Z8ua3fbU1i9fPqw0gGLF0aw6yqnTK3GO6R9CFvnlvXB0x0AIFi6JqYvQ6v+s9kuw" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script src="https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js" integrity="sha384-+mbV2IY1Zk/X1p/nWllGySJSUN8uMs+gUAN10Or95UBH0fpj6GfKgPmgC5EXieXG" crossorigin="anonymous" referrerpolicy="no-referrer"></script>
</head><body><header><div><h1>نمایشگر تصاویر MRI</h1><div class="notice">پرونده خصوصی آسا فیزیو</div></div><a href="/booking/reserve">بازگشت به پنل بیمار ←</a></header>
<main><div class="tools"><button id="prev" disabled>برش قبلی</button><button id="next" disabled>برش بعدی</button>
<label>مرکز <input id="center" type="range" min="-1000" max="3000" value="40"></label>
<label>عرض پنجره <input id="width" type="range" min="1" max="4000" value="400"></label>
<button id="zoomOut">− زوم</button><button id="zoomIn">+ زوم</button><button id="reset">بازنشانی</button></div>
<div class="viewer"><canvas id="image"></canvas><div id="status" class="status">در حال بارگذاری فایل MRI…</div></div>
<div class="bar"><span id="sliceText">برش —</span><input id="slice" type="range" min="0" max="0" value="0" disabled></div><div id="meta" class="meta"></div>
<p class="notice">در این نسخه، نمایش مستقیم برای DICOM بدون فشرده‌سازی فراهم است. اگر فایل از فشرده‌سازی اختصاصی استفاده کند، ممکن است نمایش داده نشود؛ فایل اصلی محفوظ می‌ماند. این ابزار جایگزین تفسیر پزشک نیست.</p></main>
<script>
(async function(){
var $=function(id){return document.getElementById(id)},canvas=$('image'),ctx=canvas.getContext('2d'),slices=[],idx=0,zoom=1,center=40,width=400;
function say(t){$('status').textContent=t;$('status').style.display='block';canvas.style.display='none'}
function val(ds,tag,def){try{var x=ds.string(tag);return x===undefined?def:x}catch(e){return def}}
function num(ds,tag,def){try{return ds.uint16(tag)}catch(e){var n=parseInt(val(ds,tag,''),10);return isNaN(n)?def:n}}
function safeLabel(name){var n=String(name||'').replace(/\\/g,'/').split('/').pop()||'slice';return n.replace(/[^A-Za-z0-9._-]/g,'').slice(0,40)||'slice'}
function parse(buf,name){var arr=new Uint8Array(buf),ds;try{ds=dicomParser.parseDicom(arr)}catch(e){return null}
var pe=ds.elements.x7fe00010,rows=num(ds,'x00280010',0),cols=num(ds,'x00280011',0),bits=num(ds,'x00280100',16),signed=num(ds,'x00280103',0)===1;
if(!pe||!rows||!cols||![8,16].includes(bits)||rows*cols>16000000)return null;
var ts=val(ds,'x00020010','1.2.840.10008.1.2');if(!['1.2.840.10008.1.2','1.2.840.10008.1.2.1'].includes(ts))return {unsupported:true,label:safeLabel(name)};
var count=rows*cols,bpc=bits/8;if(pe.length<count*bpc)return null;var dv=new DataView(arr.buffer,arr.byteOffset,arr.byteLength),vals=new Float64Array(count),mn=Infinity,mx=-Infinity;
for(var i=0;i<count;i++){var v=bits===8?dv.getUint8(pe.dataOffset+i):dv.getUint16(pe.dataOffset+i*2,true);if(bits===16&&signed&&v>32767)v-=65536;vals[i]=v;mn=Math.min(mn,v);mx=Math.max(mx,v)}
var ipp=val(ds,'x00200032','').split('\\').map(Number);
/* Keep only geometry/window tags in memory for render — never surface PatientName/ID/etc. */
return {label:safeLabel(name),wc:val(ds,'x00281050','40'),ww:val(ds,'x00281051','400'),rows:rows,cols:cols,vals:vals,min:mn,max:mx,ipp:ipp,inst:num(ds,'x00200013',0),photo:val(ds,'x00280004','MONOCHROME2'),slope:Number(val(ds,'x00281053','1'))||1,intercept:Number(val(ds,'x00281052','0'))||0};}
function draw(){var s=slices[idx];if(!s||s.unsupported){say('این فایل فشرده یا ناسازگار است و در این نسخه قابل نمایش نیست. فایل اصلی محفوظ مانده است.');return}
canvas.style.display='block';$('status').style.display='none';canvas.width=s.cols;canvas.height=s.rows;var out=ctx.createImageData(s.cols,s.rows),lo=center-width/2,hi=center+width/2;
for(var i=0;i<s.vals.length;i++){var v=s.vals[i]*s.slope+s.intercept;v=Math.max(0,Math.min(255,Math.round((v-lo)/Math.max(1,hi-lo)*255)));if(s.photo==='MONOCHROME1')v=255-v;var j=i*4;out.data[j]=out.data[j+1]=out.data[j+2]=v;out.data[j+3]=255}
ctx.putImageData(out,0,0);canvas.style.width=(s.cols*zoom)+'px';canvas.style.height=(s.rows*zoom)+'px';$('sliceText').textContent='برش '+(idx+1)+' از '+slices.length;$('slice').value=idx;
$('prev').disabled=idx===0;$('next').disabled=idx>=slices.length-1;$('meta').textContent=s.cols+' × '+s.rows+' · برش '+(s.inst||(idx+1));}
function setIdx(n){idx=Math.max(0,Math.min(slices.length-1,n));draw()}
$('prev').onclick=function(){setIdx(idx-1)};$('next').onclick=function(){setIdx(idx+1)};$('slice').oninput=function(e){setIdx(+e.target.value)};
$('center').oninput=function(e){center=+e.target.value;draw()};$('width').oninput=function(e){width=+e.target.value;draw()};
$('zoomIn').onclick=function(){zoom=Math.min(5,zoom*1.2);draw()};$('zoomOut').onclick=function(){zoom=Math.max(.2,zoom/1.2);draw()};
$('reset').onclick=function(){zoom=1;center=40;width=400;$('center').value=center;$('width').value=width;draw()};
canvas.addEventListener('wheel',function(e){e.preventDefault();setIdx(idx+(e.deltaY>0?1:-1))},{passive:false});
try{var res=await fetch('/api/portal/mri/file',{credentials:'same-origin',cache:'no-store'});if(!res.ok)throw Error('دریافت فایل MRI ناموفق بود.');
var blob=await res.blob(),buf=await blob.arrayBuffer(),list=[];
var ZIP_MAX_ENTRIES=__ZIP_MAX_ENTRIES__,ZIP_MAX_UNCOMPRESSED=__ZIP_MAX_UNCOMPRESSED__,ZIP_MAX_RATIO=__ZIP_MAX_RATIO__;
if(new Uint8Array(buf,0,Math.min(4,buf.byteLength))[0]===0x50){var zip=await JSZip.loadAsync(buf),entries=Object.values(zip.files).filter(function(f){return !f.dir});
if(entries.length>ZIP_MAX_ENTRIES)throw Error('تعداد فایل‌های ZIP بیش از حد مجاز است.');
var total=0,readable=[];
for(var k=0;k<entries.length;k++){var f=entries[k],path=String(f.name||'').replace(/\\/g,'/');
if(!path||path.charAt(0)==='/'||path.indexOf('..')>=0||(path.length>=2&&path.charAt(1)===':'))throw Error('مسیر فایل داخل ZIP مجاز نیست.');
var meta=f._data;if(!meta||typeof meta.uncompressedSize!=='number'||!isFinite(meta.uncompressedSize)||meta.uncompressedSize<0||typeof meta.compressedSize!=='number'||!isFinite(meta.compressedSize)||meta.compressedSize<0)throw Error('metadata فایل ZIP نامعتبر است.');
if(meta.compressedSize>0&&meta.uncompressedSize>meta.compressedSize*ZIP_MAX_RATIO)throw Error('نسبت فشرده‌سازی ZIP بیش از حد مجاز است.');
total+=meta.uncompressedSize;if(total>ZIP_MAX_UNCOMPRESSED)throw Error('حجم بازشده ZIP بیش از حد مجاز است.');
if(/\.(pdf|jpg|jpeg|png|webp)$/i.test(path))continue;readable.push({file:f,path:path,expected:meta.uncompressedSize});}
for(var i=0;i<readable.length;i++){var ent=readable[i],b;try{b=await ent.file.async('arraybuffer')}catch(err){throw Error('خواندن ZIP ممکن نشد (رمزگذاری یا ساختار غیرعادی).')}
if(b.byteLength!==ent.expected)throw Error('اندازه استخراج‌شده با metadata ZIP هم‌خوان نیست.');var item=parse(b,ent.path);if(item)list.push(item)}
}else{var one=parse(buf,'slice');if(one)list.push(one)}
if(!list.length)throw Error('فایل DICOM قابل‌خواندن پیدا نشد.');list.sort(function(a,b){if(a.unsupported||b.unsupported)return 0;var az=a.ipp[2],bz=b.ipp[2];return Number.isFinite(az)&&Number.isFinite(bz)?az-bz:(a.inst||0)-(b.inst||0)});
slices=list;var first=list.find(function(x){return !x.unsupported});if(first){center=Number(String(first.wc||'40').split('\\')[0])||Math.round((first.min+first.max)/2);width=Number(String(first.ww||'400').split('\\')[0])||Math.max(1,first.max-first.min);$('center').value=Math.max(-1000,Math.min(3000,center));$('width').value=Math.max(1,Math.min(4000,width))}
$('slice').max=slices.length-1;$('slice').disabled=false;setIdx(0);
}catch(e){say(e.message||'خواندن فایل MRI ممکن نشد.')}
})();
</script></body></html>"""
    html = (
        html.replace("__ZIP_MAX_ENTRIES__", str(patients.MRI_ZIP_MAX_ENTRIES))
        .replace("__ZIP_MAX_UNCOMPRESSED__", str(patients.MRI_ZIP_MAX_UNCOMPRESSED))
        .replace("__ZIP_MAX_RATIO__", str(patients.MRI_ZIP_MAX_RATIO))
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/mri/file")
async def mri_file(request: Request) -> Response:
    p = patients.require(current_patient(request))
    path = patients.upload_path(p.get("mri_file", ""))
    if not path:
        raise NotFound("فایل MRI در پرونده پیدا نشد.")
    return Response(
        content=path.read_bytes(),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'attachment; filename="mri-upload"',
        },
    )
