"""Patient-facing booking API: register, login, slot grid, reserve."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Body, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from . import clinical, messages, metrics, patients, ratelimit, repo, scheduling, schemas, sms
from .config import settings
from .errors import BadRequest, Forbidden, NotFound
from .jalali import to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM

router = APIRouter(prefix="/api/portal", tags=["portal"])


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

    photo = patients.save_upload(med_photo, prefix=data.national_id[-4:])
    mri = patients.save_upload(mri_file, prefix=f"mri-{data.national_id[-4:]}")
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
        return Response(content=html, media_type="text/html; charset=utf-8", headers={"Cache-Control":"no-store"})
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
<script src="https://cdn.jsdelivr.net/npm/dicom-parser@1.8.21/dist/dicomParser.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/jszip@3.10.1/dist/jszip.min.js"></script>
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
function parse(buf,name){var arr=new Uint8Array(buf),ds;try{ds=dicomParser.parseDicom(arr)}catch(e){return null}
var pe=ds.elements.x7fe00010,rows=num(ds,'x00280010',0),cols=num(ds,'x00280011',0),bits=num(ds,'x00280100',16),signed=num(ds,'x00280103',0)===1;
if(!pe||!rows||!cols||![8,16].includes(bits)||rows*cols>16000000)return null;
var ts=val(ds,'x00020010','1.2.840.10008.1.2');if(!['1.2.840.10008.1.2','1.2.840.10008.1.2.1'].includes(ts))return {unsupported:true,name:name};
var count=rows*cols,bpc=bits/8;if(pe.length<count*bpc)return null;var dv=new DataView(arr.buffer,arr.byteOffset,arr.byteLength),vals=new Float64Array(count),mn=Infinity,mx=-Infinity;
for(var i=0;i<count;i++){var v=bits===8?dv.getUint8(pe.dataOffset+i):dv.getUint16(pe.dataOffset+i*2,true);if(bits===16&&signed&&v>32767)v-=65536;vals[i]=v;mn=Math.min(mn,v);mx=Math.max(mx,v)}
var ipp=val(ds,'x00200032','').split('\\\\').map(Number);
return {name:name,ds:ds,rows:rows,cols:cols,vals:vals,min:mn,max:mx,ipp:ipp,inst:num(ds,'x00200013',0),photo:val(ds,'x00280004','MONOCHROME2'),slope:Number(val(ds,'x00281053','1'))||1,intercept:Number(val(ds,'x00281052','0'))||0};}
function draw(){var s=slices[idx];if(!s||s.unsupported){say('این فایل فشرده یا ناسازگار است و در این نسخه قابل نمایش نیست. فایل اصلی محفوظ مانده است.');return}
canvas.style.display='block';$('status').style.display='none';canvas.width=s.cols;canvas.height=s.rows;var out=ctx.createImageData(s.cols,s.rows),lo=center-width/2,hi=center+width/2;
for(var i=0;i<s.vals.length;i++){var v=s.vals[i]*s.slope+s.intercept;v=Math.max(0,Math.min(255,Math.round((v-lo)/Math.max(1,hi-lo)*255)));if(s.photo==='MONOCHROME1')v=255-v;var j=i*4;out.data[j]=out.data[j+1]=out.data[j+2]=v;out.data[j+3]=255}
ctx.putImageData(out,0,0);canvas.style.width=(s.cols*zoom)+'px';canvas.style.height=(s.rows*zoom)+'px';$('sliceText').textContent='برش '+(idx+1)+' از '+slices.length;$('slice').value=idx;
$('prev').disabled=idx===0;$('next').disabled=idx>=slices.length-1;$('meta').textContent=s.cols+' × '+s.rows+' · '+s.name+' · Instance '+(s.inst||'—');}
function setIdx(n){idx=Math.max(0,Math.min(slices.length-1,n));draw()}
$('prev').onclick=function(){setIdx(idx-1)};$('next').onclick=function(){setIdx(idx+1)};$('slice').oninput=function(e){setIdx(+e.target.value)};
$('center').oninput=function(e){center=+e.target.value;draw()};$('width').oninput=function(e){width=+e.target.value;draw()};
$('zoomIn').onclick=function(){zoom=Math.min(5,zoom*1.2);draw()};$('zoomOut').onclick=function(){zoom=Math.max(.2,zoom/1.2);draw()};
$('reset').onclick=function(){zoom=1;center=40;width=400;$('center').value=center;$('width').value=width;draw()};
canvas.addEventListener('wheel',function(e){e.preventDefault();setIdx(idx+(e.deltaY>0?1:-1))},{passive:false});
try{var res=await fetch('/api/portal/mri/file',{credentials:'same-origin',cache:'no-store'});if(!res.ok)throw Error('دریافت فایل MRI ناموفق بود.');
var blob=await res.blob(),buf=await blob.arrayBuffer(),list=[];
if(new Uint8Array(buf,0,Math.min(4,buf.byteLength))[0]===0x50){var zip=await JSZip.loadAsync(buf),entries=Object.values(zip.files).filter(function(f){return !f.dir});if(entries.length>1000)throw Error('تعداد فایل‌های ZIP بیش از حد مجاز است.');var total=0;
for(var k=0;k<entries.length;k++){var f=entries[k];if(/\.(pdf|jpg|jpeg|png|webp)$/i.test(f.name))continue;var b=await f.async('arraybuffer');total+=b.byteLength;if(total>500*1024*1024)throw Error('حجم بازشده ZIP بیش از حد مجاز است.');var item=parse(b,f.name);if(item)list.push(item)}
}else{var one=parse(buf,'MRI DICOM');if(one)list.push(one)}
if(!list.length)throw Error('فایل DICOM قابل‌خواندن پیدا نشد.');list.sort(function(a,b){if(a.unsupported||b.unsupported)return 0;var az=a.ipp[2],bz=b.ipp[2];return Number.isFinite(az)&&Number.isFinite(bz)?az-bz:(a.inst||0)-(b.inst||0)});
slices=list;var first=list.find(function(x){return !x.unsupported});if(first){center=Number(val(first.ds,'x00281050','40').split('\\\\')[0])||Math.round((first.min+first.max)/2);width=Number(val(first.ds,'x00281051','400').split('\\\\')[0])||Math.max(1,first.max-first.min);$('center').value=Math.max(-1000,Math.min(3000,center));$('width').value=Math.max(1,Math.min(4000,width))}
$('slice').max=slices.length-1;$('slice').disabled=false;setIdx(0);
}catch(e){say(e.message||'خواندن فایل MRI ممکن نشد.')}
})();
</script></body></html>"""
    return Response(content=html, media_type="text/html; charset=utf-8",
                    headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})


@router.get("/mri/file")
async def mri_file(request: Request) -> Response:
    p = patients.require(current_patient(request))
    path = patients.upload_path(p.get("mri_file", ""))
    if not path:
        raise NotFound("فایل MRI در پرونده پیدا نشد.")
    return Response(content=path.read_bytes(), media_type="application/octet-stream",
                    headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff",
                             "Content-Disposition":'attachment; filename="mri-upload"'})
