"""Server-rendered patient portal pages.

They deliberately reuse the site's own stylesheet (`public/style.<hash>.css`), so the
portal inherits the exact theme, buttons, fields and typography of the site. Nothing in
the site's own template or copy is touched.
"""

from __future__ import annotations

import functools
import json
import pathlib
import re

from .config import settings

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SHELL = HERE / "templates" / "portal.html"


@functools.lru_cache(maxsize=1)
def logo_path() -> str:
    svg = (ROOT / "assets" / "mark.svg").read_text(encoding="utf-8")
    m = re.search(r'<path d="([^"]+)"', svg)
    return m.group(1) if m else ""


def css_href() -> str:
    """The fingerprinted stylesheet produced by build_static.py."""
    manifest = settings.STATIC_DIR / "asset-manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if "style.css" in data:
                return "/" + data["style.css"]
        except Exception:  # noqa: S110,BLE001 - a broken manifest falls back to the glob
            pass
    hits = sorted(settings.STATIC_DIR.glob("style.*.css"))
    return "/" + hits[0].name if hits else "/style.css"


def render(title: str, body: str, script: str = "") -> str:
    shell = SHELL.read_text(encoding="utf-8")
    return (
        shell.replace("{{TITLE}}", title)
        .replace("{{CSS}}", css_href())
        .replace("{{MARK}}", logo_path())
        .replace("{{BODY}}", body)
        .replace("{{SCRIPT}}", COMMON_JS + script)
    )


# ==========================================================================
# shared client helper
# ==========================================================================
COMMON_JS = r"""
'use strict';
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const fa=n=>Number(n||0).toLocaleString('fa-IR');
const faDigits=s=>String(s).replace(/\d/g,d=>'۰۱۲۳۴۵۶۷۸۹'[d]);
const PHONE_TXT='0902 46 48 159';
let toastTimer=null;
function flash(msg,isErr){
  const t=$('#toast'); if(!t) return;
  t.textContent=msg; t.classList.toggle('is-err',!!isErr); t.classList.add('show');
  clearTimeout(toastTimer); toastTimer=setTimeout(()=>t.classList.remove('show'),5200);
}
async function api(path,opts={}){
  const o=Object.assign({headers:{},credentials:'same-origin'},opts);
  if(o.body && !(o.body instanceof FormData) && typeof o.body!=='string'){
    o.body=JSON.stringify(o.body); o.headers['Content-Type']='application/json';
  }
  let res;
  try{ res=await fetch(path,o); }
  catch(e){ return {ok:false,error:{message:'ارتباط با سرور برقرار نشد. اتصال اینترنت را بررسی کنید یا تماس بگیرید: '+PHONE_TXT}}; }
  if(res.status===204) return {ok:true,data:null};
  let body=null; try{ body=await res.json(); }catch(e){}
  if(!res.ok){
    const err=(body&&body.error)||{message:'خطای ناشناخته ('+res.status+')'};
    err.status=res.status; return {ok:false,error:err};
  }
  return {ok:true,data:body};
}
function clearErrs(ids){ids.forEach(id=>{const f=document.getElementById(id);
  if(f&&f.closest('.field'))f.closest('.field').classList.remove('is-bad');
  const m=document.getElementById('e-'+id); if(m)m.textContent='';});}
function markErr(id,msg){const f=document.getElementById(id); if(!f)return;
  const w=f.closest('.field'); if(w)w.classList.add('is-bad');
  const m=document.getElementById('e-'+id); if(m)m.textContent=msg;}
async function withBtn(btn,label,fn){
  const old=btn.innerHTML; btn.classList.add('is-loading'); btn.disabled=true;
  btn.setAttribute('aria-busy','true'); btn.innerHTML=label;
  try{ await fn(); } finally {
    btn.classList.remove('is-loading'); btn.disabled=false;
    btn.removeAttribute('aria-busy'); btn.innerHTML=old;
  }
}
function errBox(sel,err,retry){
  const el=$(sel); if(!el) return;
  el.innerHTML='';
  el.append(document.createTextNode(err.message||'خطایی رخ داد.'));
  if(err.trace_id){const s=document.createElement('span');s.className='sub';
    s.textContent='کد پیگیری: '+err.trace_id;el.appendChild(s);}
  if(retry){const b=document.createElement('button');b.className='btn btn-ghost';
    b.style.cssText='margin-top:12px;padding:8px 16px;font-size:.8rem';
    b.textContent='تلاش دوباره';b.onclick=retry;
    el.appendChild(document.createElement('br'));el.appendChild(b);}
  el.classList.remove('hidden');
}
/* live Jalali date masking for birth-date inputs: 1370/05/03 */
function attachDateMask(el){
  if(!el) return;
  el.addEventListener('input',()=>{
    let v=el.value.replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
                  .replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/\D/g,'').slice(0,8);
    let out=v.slice(0,4);
    if(v.length>4) out+='/'+v.slice(4,6);
    if(v.length>6) out+='/'+v.slice(6,8);
    el.value=out;
  });
}
(async function initWho(){
  const badge=$('#whoBadge'), out=$('#outBtn');
  if(!badge) return;
  const r=await api('/api/portal/me');
  if(r.ok){
    badge.textContent=r.data.patient.full_name;
    if(out){out.classList.remove('hidden');
      out.onclick=async()=>{await api('/api/portal/logout',{method:'POST'});location.href='/booking';};}
    window.__patient=r.data;
  } else { badge.textContent='رزرو نوبت آنلاین'; }
  document.dispatchEvent(new CustomEvent('who',{detail:window.__patient||null}));
})();
"""


# ==========================================================================
# 1 — entry
# ==========================================================================
ENTRY_BODY = """
<div class="pcard">
  <h1 class="ptitle">رزرو نوبت فیزیوتراپی</h1>
  <p class="psub">
    برای گرفتن نوبت، ابتدا مشخص کنید بار اول است یا قبلاً در آسا فیزیو پرونده دارید.
    زمان‌های کلینیک هر روز از <b>۱۶:۰۰ تا ۲۲:۰۰</b> و هر نوبت نیم‌ساعت است.
  </p>
  <div class="choice">
    <a href="/booking/register">
      <div class="ico"><svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6M22 11h-6"/></svg></div>
      <h3>بار اول است — ثبت‌نام</h3>
      <p>اطلاعات پرونده‌ی خود را یک بار ثبت کنید: نام، تاریخ تولد، شماره موبایل،
         لینک MRI، نام پزشک ارتوپد و عکس داروهای مصرفی.</p>
      <span class="go">شروع ثبت‌نام
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
      </span>
    </a>
    <a href="/booking/login">
      <div class="ico"><svg viewBox="0 0 24 24"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5M15 12H3"/></svg></div>
      <h3>قبلاً ثبت‌نام کرده‌ام</h3>
      <p>با کد ملی و تاریخ تولدی که هنگام ثبت‌نام وارد کرده‌اید وارد شوید و
         مستقیم به جدول انتخاب نوبت بروید.</p>
      <span class="go">ورود به پرونده
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
      </span>
    </a>
  </div>
  <a class="backlink" href="/">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>
    بازگشت به سایت
  </a>
</div>
"""

ENTRY_JS = """
document.addEventListener('who',e=>{
  if(e.detail){
    const c=document.querySelector('.choice');
    const d=document.createElement('div');
    d.className='state empty';
    d.style.cssText='grid-column:1/-1;border-style:solid;border-color:rgba(45,212,191,.3)';
    d.innerHTML='شما وارد شده‌اید. <a href="/booking/reserve" style="color:var(--glow)">'+
                'ادامه به انتخاب نوبت ←</a>';
    c.parentNode.insertBefore(d,c);
  }
});
"""


# ==========================================================================
# 2 — registration
# ==========================================================================
REGISTER_BODY = """
<div class="pcard">
  <div class="steps"><i class="on"></i><i></i><i></i><span>۱ از ۳ — ثبت اطلاعات</span></div>
  <h1 class="ptitle">ثبت‌نام بیمار جدید</h1>
  <p class="psub">
    این اطلاعات فقط یک بار گرفته می‌شود و در پرونده‌ی شما می‌ماند. پس از ثبت،
    پیامک تأیید برای شما ارسال می‌شود و مستقیم به جدول انتخاب نوبت می‌روید.
  </p>

  <form id="regForm" novalidate enctype="multipart/form-data">
    <div aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden">
      <label for="website">Website</label><input type="text" id="website" tabindex="-1" autocomplete="off">
    </div>

    <div class="field">
      <input type="text" id="full_name" placeholder=" " required minlength="3" maxlength="80" autocomplete="name">
      <label for="full_name">نام و نام خانوادگی</label>
      <p class="err-msg" id="e-full_name"></p>
    </div>

    <div class="grid2">
      <div class="field">
        <input type="text" id="national_id" placeholder=" " required inputmode="numeric"
               maxlength="10" autocomplete="off" dir="ltr" style="text-align:right">
        <label for="national_id">کد ملی (۱۰ رقم)</label>
        <p class="hint">برای ورودهای بعدی لازم است.</p>
        <p class="err-msg" id="e-national_id"></p>
      </div>
      <div class="field">
        <input type="text" id="birth_date" placeholder=" " required inputmode="numeric"
               maxlength="10" dir="ltr" style="text-align:right">
        <label for="birth_date">تاریخ تولد — سال/ماه/روز</label>
        <p class="hint">مثال: ۱۳۷۰/۰۵/۰۳</p>
        <p class="err-msg" id="e-birth_date"></p>
      </div>
    </div>

    <div class="field">
      <input type="tel" id="phone" placeholder=" " required inputmode="numeric"
             maxlength="20" autocomplete="tel" dir="ltr" style="text-align:right">
      <label for="phone">شماره موبایل</label>
      <p class="hint">پیامک تأیید نوبت به همین شماره ارسال می‌شود.</p>
      <p class="err-msg" id="e-phone"></p>
    </div>

    <div class="grid2">
      <div class="field">
        <input type="url" id="mri_link" placeholder=" " maxlength="500" dir="ltr" style="text-align:left">
        <label for="mri_link">لینک MRI (اختیاری)</label>
        <p class="hint">لینک گوگل‌درایو، آی‌کلود یا هر فضای اشتراکی.</p>
        <p class="err-msg" id="e-mri_link"></p>
      </div>
      <div class="field">
        <input type="text" id="ortho_doctor" placeholder=" " maxlength="80">
        <label for="ortho_doctor">نام پزشک ارتوپد (اختیاری)</label>
        <p class="err-msg" id="e-ortho_doctor"></p>
      </div>
    </div>

    <div class="grid2" style="margin-top:16px">
      <label class="filebox" id="mriBox" for="mri_file">
        <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 15l5-5 4 4 3-3 6 6"/><circle cx="9" cy="8" r="1.4"/></svg>
        <b id="mriLabel">ارسال عکس MRI</b>
        <span id="mriHint">عکس یا PDF — حداکثر ۵ مگابایت (اختیاری)</span>
        <input type="file" id="mri_file" accept="image/jpeg,image/png,image/webp,application/pdf">
      </label>
      <label class="filebox" id="fileBox" for="med_photo">
        <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5M12 3v12"/></svg>
        <b id="fileLabel">ارسال عکس داروهای مصرفی</b>
        <span id="fileHint">عکس یا PDF — حداکثر ۵ مگابایت (اختیاری)</span>
        <input type="file" id="med_photo" accept="image/jpeg,image/png,image/webp,application/pdf">
      </label>
    </div>

    <div style="margin-top:22px">
      <button class="btn btn-primary" type="submit" id="regBtn" style="width:100%;padding:15px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 6L9 17l-5-5"/></svg>
        ثبت اطلاعات و ادامه
      </button>
    </div>
    <p class="hint" style="text-align:center;margin-top:12px">
      با ثبت این فرم، پیامک «اطلاعات شما ثبت شد» برای شما ارسال می‌شود.
    </p>
  </form>

  <a class="backlink" href="/booking">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>
    بازگشت
  </a>
</div>
"""

REGISTER_JS = r"""
const IDS=['full_name','national_id','birth_date','phone','mri_link','ortho_doctor'];
attachDateMask($('#birth_date'));
['national_id','phone'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener('input',()=>{el.value=el.value
    .replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
    .replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/[^\d+]/g,'');});
});
function wireFile(inputId,boxId,labelId,hintId,idleLabel){
  const inp=document.getElementById(inputId), box=document.getElementById(boxId);
  if(!inp) return inp;
  inp.addEventListener('change',()=>{
    const f=inp.files[0];
    if(!f){box.classList.remove('has');
      document.getElementById(labelId).textContent=idleLabel;
      document.getElementById(hintId).textContent='عکس یا PDF — حداکثر ۵ مگابایت (اختیاری)';
      return;}
    if(f.size>5*1024*1024){inp.value='';flash('حجم فایل بیشتر از ۵ مگابایت است.',true);return;}
    box.classList.add('has');
    document.getElementById(labelId).textContent='✓ '+f.name;
    document.getElementById(hintId).textContent=
      faDigits((f.size/1024).toFixed(0))+' کیلوبایت — برای تغییر کلیک کنید';
  });
  return inp;
}
const fileInput=wireFile('med_photo','fileBox','fileLabel','fileHint','ارسال عکس داروهای مصرفی');
const mriInput =wireFile('mri_file','mriBox','mriLabel','mriHint','ارسال عکس MRI');

function nidValid(v){
  if(!/^\d{10}$/.test(v)||/^(\d)\1{9}$/.test(v))return false;
  let s=0; for(let i=0;i<9;i++)s+=+v[i]*(10-i);
  const r=s%11, c=+v[9];
  return r<2 ? c===r : c===11-r;
}
function localCheck(){
  let first=null;
  const nm=$('#full_name').value.trim();
  if(nm.split(/\s+/).filter(Boolean).length<2){markErr('full_name','نام و نام خانوادگی را کامل وارد کنید.');first='full_name';}
  const nid=$('#national_id').value.trim();
  if(!nidValid(nid)){markErr('national_id','کد ملی معتبر نیست.');first=first||'national_id';}
  const bd=$('#birth_date').value.trim();
  if(!/^\d{4}\/\d{1,2}\/\d{1,2}$/.test(bd)){markErr('birth_date','تاریخ را به شکل ۱۳۷۰/۰۵/۰۳ وارد کنید.');first=first||'birth_date';}
  const ph=$('#phone').value.trim();
  if(!/^(09\d{9}|\+989\d{9}|9\d{9})$/.test(ph)){markErr('phone','شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.');first=first||'phone';}
  const mri=$('#mri_link').value.trim();
  if(mri && !/^https?:\/\/\S+\.\S+/.test(mri)){markErr('mri_link','لینک باید با http:// یا https:// شروع شود.');first=first||'mri_link';}
  return first;
}

let sending=false;
$('#regForm').addEventListener('submit',async e=>{
  e.preventDefault();
  if(sending)return;
  clearErrs(IDS);
  const bad=localCheck();
  if(bad){document.getElementById(bad).focus();flash('لطفاً خطاهای فرم را برطرف کنید.',true);return;}
  sending=true;
  await withBtn($('#regBtn'),'در حال ثبت…',async()=>{
    const fd=new FormData();
    IDS.forEach(id=>fd.append(id,document.getElementById(id).value.trim()));
    fd.append('website',$('#website').value);
    if(fileInput&&fileInput.files[0]) fd.append('med_photo',fileInput.files[0]);
    if(mriInput&&mriInput.files[0]) fd.append('mri_file',mriInput.files[0]);
    const r=await api('/api/portal/register',{method:'POST',body:fd});
    if(!r.ok){
      const err=r.error;
      if(err.fields){let f=null;for(const k in err.fields){
        if(document.getElementById(k)){markErr(k,err.fields[k]);f=f||k;}}
        if(f)document.getElementById(f).focus();}
      if(err.code==='already_registered'){
        flash(err.message,true);
        setTimeout(()=>location.href='/booking/login',2200);
      } else {
        flash((err.message||'ثبت نشد.')+(err.trace_id?' (کد: '+err.trace_id+')':''),true);
      }
      return;
    }
    flash(r.data.sms_sent
      ? 'ثبت شد ✓ پیامک تأیید برای شما ارسال شد'
      : 'ثبت شد ✓ (ارسال پیامک با تأخیر انجام می‌شود)');
    setTimeout(()=>location.href=r.data.redirect||'/booking/reserve',1300);
  });
  sending=false;
});
"""


# ==========================================================================
# 3 — returning patient login
# ==========================================================================
LOGIN_BODY = """
<div class="pcard" style="max-width:520px;margin-inline:auto">
  <h1 class="ptitle">ورود بیماران قبلی</h1>
  <p class="psub">
    کد ملی و تاریخ تولد باید دقیقاً با چیزی که هنگام ثبت‌نام وارد کرده‌اید یکی باشد.
  </p>

  <form id="loginForm" novalidate>
    <div class="field">
      <input type="text" id="national_id" placeholder=" " required inputmode="numeric"
             maxlength="10" dir="ltr" style="text-align:right" autocomplete="off">
      <label for="national_id">کد ملی</label>
      <p class="err-msg" id="e-national_id"></p>
    </div>
    <div class="field">
      <input type="text" id="birth_date" placeholder=" " required inputmode="numeric"
             maxlength="10" dir="ltr" style="text-align:right">
      <label for="birth_date">تاریخ تولد — سال/ماه/روز</label>
      <p class="hint">مثال: ۱۳۷۰/۰۵/۰۳</p>
      <p class="err-msg" id="e-birth_date"></p>
    </div>
    <div style="margin-top:20px">
      <button class="btn btn-primary" type="submit" id="loginBtn" style="width:100%;padding:15px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 17l5-5-5-5M15 12H3M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/></svg>
        ورود به پرونده
      </button>
    </div>
  </form>
  <div id="loginErr" class="state error hidden" style="margin-top:16px"></div>
  <p class="hint" style="text-align:center;margin-top:16px">
    بار اول است؟ <a href="/booking/register" style="color:var(--glow)">ثبت‌نام کنید</a>
  </p>
  <a class="backlink" href="/booking">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>
    بازگشت
  </a>
</div>
"""

LOGIN_JS = r"""
attachDateMask($('#birth_date'));
$('#national_id').addEventListener('input',e=>{
  e.target.value=e.target.value.replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
    .replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d)).replace(/\D/g,'');
});
$('#loginForm').addEventListener('submit',async e=>{
  e.preventDefault();
  clearErrs(['national_id','birth_date']);
  $('#loginErr').classList.add('hidden');
  let bad=null;
  if(!/^\d{10}$/.test($('#national_id').value.trim())){markErr('national_id','کد ملی باید ۱۰ رقم باشد.');bad='national_id';}
  if(!/^\d{4}\/\d{1,2}\/\d{1,2}$/.test($('#birth_date').value.trim())){markErr('birth_date','تاریخ را به شکل ۱۳۷۰/۰۵/۰۳ وارد کنید.');bad=bad||'birth_date';}
  if(bad){document.getElementById(bad).focus();return;}
  await withBtn($('#loginBtn'),'در حال بررسی…',async()=>{
    const r=await api('/api/portal/login',{method:'POST',body:{
      national_id:$('#national_id').value.trim(), birth_date:$('#birth_date').value.trim()}});
    if(!r.ok){errBox('#loginErr',r.error);return;}
    flash('خوش آمدید '+r.data.patient.full_name);
    setTimeout(()=>location.href=r.data.redirect||'/booking/reserve',900);
  });
});
"""


# ==========================================================================
# 4 — slot picker
# ==========================================================================
RESERVE_BODY = """
<div class="pcard">
  <div class="steps"><i class="on"></i><i class="on"></i><i></i><span>۲ از ۳ — انتخاب زمان</span></div>
  <h1 class="ptitle">انتخاب تاریخ و ساعت مراجعه</h1>
  <p class="psub">
    کلینیک هر روز از ساعت <b>۱۶:۰۰ تا ۲۲:۰۰</b> فعال است و هر نوبت نیم‌ساعت طول می‌کشد.
    ساعت‌هایی که کم‌رنگ هستند پر شده‌اند و قابل انتخاب نیستند.
  </p>

  <div id="dayLoading"><div class="skel" style="height:72px"></div></div>
  <div id="dayError" class="state error hidden"></div>
  <div class="dayrow hidden" id="dayRow"></div>

  <div id="gridHead" class="hidden" style="display:flex;justify-content:space-between;
       align-items:baseline;flex-wrap:wrap;gap:8px;margin:10px 2px 12px">
    <b id="dayLabel" style="font-size:.98rem"></b>
    <span class="hint" id="dayFree" style="margin:0"></span>
  </div>

  <div id="slotLoading" class="hidden"><div class="spin"></div>
    <p class="state loading">در حال خواندن ساعت‌های خالی…</p></div>
  <div id="slotError" class="state error hidden"></div>
  <div id="slotEmpty" class="state empty hidden">
    برای این روز هیچ ساعت خالی باقی نمانده است.
    <span class="sub">روز دیگری را انتخاب کنید یا با ۰۹۰۲ ۴۶ ۴۸ ۱۵۹ تماس بگیرید.</span>
  </div>
  <div class="slotgrid hidden" id="slotGrid"></div>

  <div class="legend hidden" id="legend">
    <span><i></i>آزاد</span>
    <span><i class="l-on"></i>انتخاب شما</span>
    <span><i class="l-full"></i>تکمیل / گذشته</span>
  </div>

  <div class="field hidden" id="noteWrap" style="margin-top:20px">
    <textarea id="note" placeholder=" " maxlength="500"></textarea>
    <label for="note">توضیح کوتاه برای پزشک (اختیاری)</label>
  </div>

  <div id="confirmWrap" class="hidden" style="margin-top:20px">
    <div class="recap">
      <div><b id="rDate">—</b><span>تاریخ مراجعه</span></div>
      <div><b id="rTime" style="direction:ltr">—</b><span>ساعت مراجعه</span></div>
      <div><b id="rName">—</b><span>به نام</span></div>
    </div>
    <button class="btn btn-primary" id="bookBtn" style="width:100%;padding:15px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8 2v4M16 2v4M3 10h18"/><rect x="3" y="4" width="18" height="18" rx="3"/><path d="M9 16l2 2 4-4"/></svg>
      ثبت نهایی نوبت
    </button>
    <p class="hint" style="text-align:center;margin-top:10px">
      پس از ثبت، پیامک تأیید با کد پیگیری برای شما ارسال می‌شود.
    </p>
  </div>
</div>

<div class="pcard" id="myCard" style="display:none">
  <h2 class="ptitle" style="font-size:1.05rem">نوبت‌های شما</h2>
  <div class="mylist" id="myList"></div>
</div>
"""

RESERVE_JS = r"""
let DAYS=[], SEL_DATE=null, SEL_TIME=null, ME=null;

document.addEventListener('who',e=>{
  if(!e.detail){ location.href='/booking'; return; }
  ME=e.detail;
  $('#rName').textContent=ME.patient.full_name;
  renderMine();
  loadDays();
});

function renderMine(){
  const list=(ME&&ME.appointments)||[];
  const up=list.filter(a=>a.status!=='cancelled');
  if(!up.length) return;
  $('#myCard').style.display='';
  const box=$('#myList'); box.innerHTML='';
  const ST={booked:'ثبت‌شده',attended:'انجام شد',cancelled:'لغو شد',no_show:'مراجعه نشد'};
  up.forEach(a=>{
    const d=document.createElement('div'); d.className='myrow';
    const w=document.createElement('span'); w.className='w';
    w.textContent=a.label+' — ساعت '+a.slot_time;
    const p=document.createElement('span'); p.className='pill p-'+a.status;
    p.textContent=ST[a.status]||a.status;
    d.append(w,p);
    if(a.attendance==='coming'){
      const c=document.createElement('span'); c.className='pill p-coming';
      c.textContent='حضور تأیید شد'; d.appendChild(c);
    }
    const code=document.createElement('span');
    code.style.cssText='font-size:.72rem;color:var(--muted);direction:ltr;margin-inline-start:auto';
    code.textContent=a.public_id; d.appendChild(code);
    box.appendChild(d);
  });
}

async function loadDays(){
  $('#dayLoading').classList.remove('hidden');
  $('#dayError').classList.add('hidden');
  const r=await api('/api/portal/days');
  $('#dayLoading').classList.add('hidden');
  if(!r.ok) return errBox('#dayError',r.error,loadDays);
  DAYS=r.data.days;
  const row=$('#dayRow'); row.innerHTML='';
  DAYS.forEach((d,i)=>{
    const b=document.createElement('button');
    b.type='button'; b.className='daybtn'; b.dataset.date=d.date;
    const parts=d.label.split(' ');
    b.innerHTML='<span>'+parts[0]+'</span><b>'+faDigits(parts[1])+'</b><span>'+parts[2]+'</span>';
    b.onclick=()=>pickDay(d.date);
    row.appendChild(b);
  });
  row.classList.remove('hidden');
  pickDay(DAYS[0].date);
}

let dayReq=0;
async function pickDay(date){
  // wipe the previous day's buttons before awaiting, so a stale slot can never be
  // clicked while the new day is still loading
  SEL_DATE=date; SEL_TIME=null;
  const my=++dayReq;
  $('#slotGrid').innerHTML='';
  $$('.daybtn').forEach(b=>b.classList.toggle('on',b.dataset.date===date));
  $('#confirmWrap').classList.add('hidden');
  $('#noteWrap').classList.add('hidden');
  $('#slotGrid').classList.add('hidden');
  $('#slotEmpty').classList.add('hidden');
  $('#slotError').classList.add('hidden');
  $('#legend').classList.add('hidden');
  $('#slotLoading').classList.remove('hidden');

  const r=await api('/api/portal/slots?date='+encodeURIComponent(date));
  if(my!==dayReq) return;            // a newer day was picked
  $('#slotLoading').classList.add('hidden');
  if(!r.ok) return errBox('#slotError',r.error,()=>pickDay(date));

  const g=r.data;
  $('#gridHead').classList.remove('hidden');
  $('#dayLabel').textContent=g.label;
  const freeTotal=g.slots.reduce((s,x)=>s+(x.disabled?0:x.free),0);
  $('#dayFree').textContent=freeTotal
    ? faDigits(freeTotal)+' نوبت خالی از '+faDigits(g.capacity)
    : 'ظرفیت این روز تکمیل است';

  const open=g.slots.filter(s=>!s.disabled);
  if(!open.length){ $('#slotEmpty').classList.remove('hidden'); return; }

  const box=$('#slotGrid'); box.innerHTML='';
  g.slots.forEach(s=>{
    const b=document.createElement('button');
    b.type='button';
    b.className='slot'+(s.free<=3&&!s.disabled?' low':'');
    b.disabled=s.disabled;
    b.dataset.time=s.time;
    const t=document.createElement('div'); t.className='t'; t.textContent=s.time;
    const c=document.createElement('div'); c.className='c';
    c.textContent=s.full?'تکمیل':(s.past?'گذشته':faDigits(s.free)+' جای خالی');
    b.append(t,c);
    b.setAttribute('aria-label',s.time+' — '+c.textContent);
    if(!s.disabled) b.onclick=()=>pickSlot(s.time,b);
    box.appendChild(b);
  });
  box.classList.remove('hidden');
  $('#legend').classList.remove('hidden');
}

function pickSlot(time,btn){
  SEL_TIME=time;
  $$('.slot').forEach(b=>b.classList.toggle('on',b===btn));
  const day=DAYS.find(d=>d.date===SEL_DATE);
  $('#rDate').textContent=day?day.label:SEL_DATE;
  $('#rTime').textContent=time;
  $('#noteWrap').classList.remove('hidden');
  $('#confirmWrap').classList.remove('hidden');
  $('#confirmWrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}

document.addEventListener('click',async e=>{
  const btn=e.target.closest&&e.target.closest('#bookBtn');
  if(!btn) return;
  if(!SEL_DATE||!SEL_TIME){flash('ابتدا تاریخ و ساعت را انتخاب کنید.',true);return;}
  await withBtn(btn,'در حال ثبت نوبت…',async()=>{
    const r=await api('/api/portal/appointments',{method:'POST',body:{
      slot_date:SEL_DATE, slot_time:SEL_TIME, note:$('#note').value.trim()}});
    if(!r.ok){
      flash((r.error.message||'ثبت نشد.')+(r.error.trace_id?' (کد: '+r.error.trace_id+')':''),true);
      if(r.error.code==='slot_full') pickDay(SEL_DATE);   // refresh the grid
      return;
    }
    flash(r.data.sms_sent?'نوبت ثبت شد ✓ پیامک تأیید ارسال شد':'نوبت ثبت شد ✓');
    setTimeout(()=>location.href=r.data.redirect,1100);
  });
});
"""


# ==========================================================================
# 5 — receipt
# ==========================================================================
DONE_BODY = """
<div class="pcard ticket">
  <div class="steps" style="margin-bottom:26px"><i class="on"></i><i class="on"></i><i class="on"></i><span>۳ از ۳ — ثبت شد</span></div>
  <div class="mark"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></div>
  <h1 class="ptitle">نوبت شما ثبت شد</h1>
  <p class="psub" id="doneSub">پیامک تأیید به شماره‌ی شما ارسال شد.</p>

  <div id="dLoading"><div class="skel" style="height:80px"></div></div>
  <div id="dError" class="state error hidden"></div>
  <div id="dBody" class="hidden">
    <div class="recap">
      <div><b id="tDate">—</b><span>تاریخ مراجعه</span></div>
      <div><b id="tTime" style="direction:ltr">—</b><span>ساعت مراجعه</span></div>
      <div><b id="tName">—</b><span>به نام</span></div>
    </div>
    <p class="hint" style="margin-bottom:4px">کد پیگیری</p>
    <div class="code" id="tCode">—</div>
  </div>

  <p class="psub" style="margin-top:26px">
    جزیره قشم، میدان ولایت (ساعت)، ساختمان محسنین، طبقه دوم<br>
    دو ساعت قبل از نوبت، پیامک یادآوری برای شما ارسال می‌شود؛
    با ارسال عدد <b>۱</b> حضور خود را قطعی کنید.
  </p>
  <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-top:8px">
    <a class="btn btn-ghost" href="/booking/reserve">نوبت دیگری بگیرم</a>
    <a class="btn btn-primary" href="/">بازگشت به سایت</a>
  </div>
</div>
"""

DONE_JS = """
document.addEventListener('who',async e=>{
  $('#dLoading').classList.add('hidden');
  if(!e.detail){ errBox('#dError',{message:'نشست شما تمام شده است.'}); return; }
  const code=new URLSearchParams(location.search).get('code');
  const list=e.detail.appointments||[];
  const a=list.find(x=>x.public_id===code)||list[0];
  if(!a){ errBox('#dError',{message:'نوبتی پیدا نشد.'}); return; }
  $('#tDate').textContent=a.label;
  $('#tTime').textContent=a.slot_time;
  $('#tName').textContent=e.detail.patient.full_name;
  $('#tCode').textContent=a.public_id;
  $('#dBody').classList.remove('hidden');
});
"""

PAGES: dict[str, tuple[str, str, str]] = {
    "entry": ("رزرو نوبت", ENTRY_BODY, ENTRY_JS),
    "register": ("ثبت‌نام بیمار", REGISTER_BODY, REGISTER_JS),
    "login": ("ورود بیماران قبلی", LOGIN_BODY, LOGIN_JS),
    "reserve": ("انتخاب نوبت", RESERVE_BODY, RESERVE_JS),
    "done": ("نوبت ثبت شد", DONE_BODY, DONE_JS),
}


def page(name: str) -> str:
    title, body, script = PAGES[name]
    return render(title, body, script)
