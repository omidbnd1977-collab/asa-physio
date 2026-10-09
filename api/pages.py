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
    svg_file = ROOT / "assets" / "mark.svg"
    # The mark is decorative; a missing file must not take the whole portal down with it.
    if not svg_file.is_file():
        return ""
    svg = svg_file.read_text(encoding="utf-8")
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
    const link=$('#msgLink');
    if(link&&location.pathname!=='/booking/messages'){
      link.classList.remove('hidden');
      const n=r.data.unread_messages||0, b=$('#msgBadge');
      if(n>0){b.textContent=fa(n);b.classList.remove('hidden');}
    }
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
                'ادامه به انتخاب نوبت ←</a> · <a href="/booking/messages" style="color:var(--glow)">'+
                'پیام‌های من ←</a>';
    c.parentNode.insertBefore(d,c);
  }
});
"""


# ==========================================================================
# 2 — registration
# ==========================================================================
REGISTER_BODY = """
<div class="pcard">
  <h1 class="ptitle">ثبت‌نام بیمار جدید</h1>
  <p class="psub">
    اطلاعات پرونده را به ترتیب زیر وارد کنید. موارد ستاره‌دار برای ثبت نوبت الزامی‌اند؛
    پس از ثبت، مستقیم به جدول انتخاب نوبت می‌روید.
  </p>

  <form id="regForm" novalidate enctype="multipart/form-data">
    <div aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden">
      <label for="website">Website</label><input type="text" id="website" tabindex="-1" autocomplete="off">
    </div>

    <div class="field">
      <input type="text" id="full_name" name="full_name" placeholder=" " required minlength="3" maxlength="80" autocomplete="name">
      <label for="full_name">نام و نام خانوادگی <span class="req">*</span></label>
      <p class="err-msg" id="e-full_name"></p>
    </div>

    <div class="field">
      <input type="text" id="national_code" name="national_code" placeholder=" " required inputmode="numeric"
             maxlength="10" autocomplete="off" dir="ltr" style="text-align:right">
      <label for="national_code">کد ملی <span class="req">*</span></label>
      <p class="hint">برای ورودهای بعدی لازم است.</p>
      <p class="err-msg" id="e-national_code"></p>
    </div>

    <div class="field">
      <label>تاریخ تولد (شمسی) <span class="req">*</span></label>
      <div class="three birth-fields">
        <input type="text" id="birth_year" name="birth_year" placeholder="سال" required inputmode="numeric" maxlength="4" dir="ltr" aria-label="سال تولد">
        <input type="text" id="birth_month" name="birth_month" placeholder="ماه" required inputmode="numeric" maxlength="2" dir="ltr" aria-label="ماه تولد">
        <input type="text" id="birth_day" name="birth_day" placeholder="روز" required inputmode="numeric" maxlength="2" dir="ltr" aria-label="روز تولد">
      </div>
      <p class="hint">سال، ماه و روز را جداگانه وارد کنید؛ مثال: ۱۳۷۰ / ۰۵ / ۰۳.</p>
      <p class="err-msg" id="e-birth_year"></p>
    </div>

    <div class="field">
      <input type="tel" id="mobile" name="mobile" placeholder=" " required inputmode="numeric"
             maxlength="20" autocomplete="tel" dir="ltr" style="text-align:right">
      <label for="mobile">شماره موبایل <span class="req">*</span></label>
      <p class="hint">پیامک تأیید نوبت به همین شماره ارسال می‌شود.</p>
      <p class="err-msg" id="e-mobile"></p>
    </div>

    <div class="field">
      <input type="url" id="mri_url" name="mri_url" placeholder=" " maxlength="500" dir="ltr" style="text-align:left">
      <label for="mri_url">لینک MRI <span class="muted">(اختیاری)</span></label>
      <p class="hint">فعلاً فقط لینک MRI دریافت می‌شود؛ آپلود مستقیم در فرم ثبت‌نام وجود ندارد.</p>
      <p class="err-msg" id="e-mri_url"></p>
    </div>

    <div class="field">
      <input type="text" id="orthopedist" name="orthopedist" placeholder=" " maxlength="80">
      <label for="orthopedist">نام پزشک ارتوپد <span class="muted">(اختیاری)</span></label>
      <p class="err-msg" id="e-orthopedist"></p>
    </div>

    <label class="filebox" id="fileBox" for="meds_photo">
      <svg viewBox="0 0 24 24" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5M12 3v12"/></svg>
      <b id="fileLabel">ارسال عکس داروهای مصرفی</b>
      <span id="fileHint">عکس یا PDF — حداکثر ۲ مگابایت (اختیاری)</span>
      <input type="file" id="meds_photo" name="meds_photo" accept="image/jpeg,image/png,image/webp,application/pdf">
    </label>

    <div class="field" style="margin-top:16px">
      <textarea id="note" name="note" maxlength="500" placeholder=" "></textarea>
      <label for="note">یادداشت <span class="muted">(اختیاری)</span></label>
      <p class="hint">توضیح کوتاه درباره مشکل یا درخواست شما؛ در مرحله رزرو برای پزشک ثبت می‌شود.</p>
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
const UI_IDS=['full_name','national_code','birth_year','birth_month','birth_day','mobile','mri_url','orthopedist','note'];
const DIGITS=s=>String(s).replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
  .replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d));
['national_code','birth_year','birth_month','birth_day','mobile'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener('input',()=>{el.value=DIGITS(el.value).replace(/[^\d+]/g,'');});
});
['birth_year','birth_month','birth_day'].forEach((id,i,all)=>{
  const el=document.getElementById(id);
  el.addEventListener('input',()=>{
    const max=el.maxLength;
    el.value=DIGITS(el.value).replace(/\D/g,'').slice(0,max);
    if(el.value.length===max && all[i+1]) all[i+1].focus();
  });
});
function wireFile(inputId,boxId,labelId,hintId,idleLabel){
  const inp=document.getElementById(inputId), box=document.getElementById(boxId);
  if(!inp) return inp;
  inp.addEventListener('change',()=>{
    const f=inp.files[0];
    if(!f){box.classList.remove('has');
      document.getElementById(labelId).textContent=idleLabel;
      document.getElementById(hintId).textContent='عکس یا PDF — حداکثر ۲ مگابایت (اختیاری)';
      return;}
    if(f.size>2*1024*1024){inp.value='';flash('حجم عکس داروها نباید بیشتر از ۲ مگابایت باشد.',true);return;}
    box.classList.add('has');
    document.getElementById(labelId).textContent='✓ '+f.name;
    document.getElementById(hintId).textContent=faDigits((f.size/1024).toFixed(0))+' کیلوبایت — برای تغییر کلیک کنید';
  });
  return inp;
}
const fileInput=wireFile('meds_photo','fileBox','fileLabel','fileHint','ارسال عکس داروهای مصرفی');
function nidValid(v){
  if(!/^\d{10}$/.test(v)||/^(\d)\1{9}$/.test(v))return false;
  let s=0; for(let i=0;i<9;i++)s+=+v[i]*(10-i);
  const r=s%11, c=+v[9]; return r<2 ? c===r : c===11-r;
}
function localCheck(){
  let first=null;
  const nm=$('#full_name').value.trim();
  if(nm.split(/\s+/).filter(Boolean).length<2){markErr('full_name','نام و نام خانوادگی را کامل وارد کنید.');first='full_name';}
  const nid=DIGITS($('#national_code').value.trim());
  if(!nidValid(nid)){markErr('national_code','کد ملی معتبر نیست.');first=first||'national_code';}
  const y=DIGITS($('#birth_year').value.trim()), m=DIGITS($('#birth_month').value.trim()), d=DIGITS($('#birth_day').value.trim());
  if(!/^\d{4}$/.test(y)||!/^\d{1,2}$/.test(m)||!/^\d{1,2}$/.test(d)){
    markErr('birth_year','سال، ماه و روز تولد را کامل وارد کنید.'); first=first||'birth_year';
  }
  const ph=DIGITS($('#mobile').value.trim());
  if(!/^(09\d{9}|\+989\d{9}|9\d{9})$/.test(ph)){markErr('mobile','شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.');first=first||'mobile';}
  const mri=$('#mri_url').value.trim();
  if(mri && !/^https?:\/\/\S+\.\S+/.test(mri)){markErr('mri_url','لینک باید با http:// یا https:// شروع شود.');first=first||'mri_url';}
  return first;
}
let sending=false;
$('#regForm').addEventListener('submit',async e=>{
  e.preventDefault(); if(sending)return;
  clearErrs(UI_IDS.concat(['birth_date']));
  const bad=localCheck();
  if(bad){document.getElementById(bad).focus();flash('لطفاً خطاهای فرم را برطرف کنید.',true);return;}
  sending=true;
  await withBtn($('#regBtn'),'در حال ثبت…',async()=>{
    const birth_date=[$('#birth_year').value,$('#birth_month').value.padStart(2,'0'),$('#birth_day').value.padStart(2,'0')].join('/');
    const fd=new FormData();
    fd.append('full_name',$('#full_name').value.trim());
    fd.append('national_id',DIGITS($('#national_code').value.trim()));
    fd.append('birth_date',DIGITS(birth_date));
    fd.append('phone',DIGITS($('#mobile').value.trim()));
    fd.append('mri_link',$('#mri_url').value.trim());
    fd.append('ortho_doctor',$('#orthopedist').value.trim());
    fd.append('website',$('#website').value);
    sessionStorage.setItem('asa_booking_note',$('#note').value.trim());
    if(fileInput&&fileInput.files[0]) fd.append('med_photo',fileInput.files[0]);
    const r=await api('/api/portal/register',{method:'POST',body:fd});
    if(!r.ok){
      const err=r.error;
      if(err.fields){let f=null;for(const k in err.fields){
        const mapped={national_id:'national_code',phone:'mobile',birth_date:'birth_year',mri_link:'mri_url',ortho_doctor:'orthopedist'}[k]||k;
        if(document.getElementById(mapped)){markErr(mapped,err.fields[k]);f=f||mapped;}}
        if(f)document.getElementById(f).focus();}
      if(err.code==='already_registered'){flash(err.message,true);setTimeout(()=>location.href='/booking/login',2200);}
      else flash((err.message||'ثبت نشد.')+(err.trace_id?' (کد: '+err.trace_id+')':''),true);
      return;
    }
    flash(r.data.mri_warning ? 'ثبت شد؛ '+r.data.mri_warning : (r.data.sms_sent?'ثبت شد ✓ پیامک تأیید برای شما ارسال شد':'ثبت شد ✓ (ارسال پیامک با تأخیر انجام می‌شود)'), !!r.data.mri_warning);
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
      <input type="text" id="national_code" placeholder=" " required inputmode="numeric"
             maxlength="10" dir="ltr" style="text-align:right" autocomplete="off">
      <label for="national_code">کد ملی <span class="req">*</span></label>
      <p class="err-msg" id="e-national_code"></p>
    </div>
    <div class="field">
      <label>تاریخ تولد (شمسی) <span class="req">*</span></label>
      <div class="three birth-fields">
        <input type="text" id="birth_year" placeholder="سال" required inputmode="numeric" maxlength="4" dir="ltr" aria-label="سال تولد">
        <input type="text" id="birth_month" placeholder="ماه" required inputmode="numeric" maxlength="2" dir="ltr" aria-label="ماه تولد">
        <input type="text" id="birth_day" placeholder="روز" required inputmode="numeric" maxlength="2" dir="ltr" aria-label="روز تولد">
      </div>
      <p class="hint">سال، ماه و روز را جداگانه وارد کنید؛ مثال: ۱۳۷۰ / ۰۵ / ۰۳.</p>
      <p class="err-msg" id="e-birth_year"></p>
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
const DIGITS_LOGIN=s=>String(s).replace(/[۰-۹]/g,d=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
  .replace(/[٠-٩]/g,d=>'٠١٢٣٤٥٦٧٨٩'.indexOf(d));
['national_code','birth_year','birth_month','birth_day'].forEach(id=>{
  const el=document.getElementById(id);
  el.addEventListener('input',()=>{el.value=DIGITS_LOGIN(el.value).replace(/\D/g,'').slice(0,el.maxLength);});
});
$('#loginForm').addEventListener('submit',async e=>{
  e.preventDefault();
  clearErrs(['national_code','birth_year','birth_month','birth_day','birth_date']);
  $('#loginErr').classList.add('hidden');
  let bad=null;
  if(!/^\d{10}$/.test($('#national_code').value.trim())){markErr('national_code','کد ملی باید ۱۰ رقم باشد.');bad='national_code';}
  const y=$('#birth_year').value.trim(),m=$('#birth_month').value.trim(),d=$('#birth_day').value.trim();
  if(!/^\d{4}$/.test(y)||!/^\d{1,2}$/.test(m)||!/^\d{1,2}$/.test(d)){markErr('birth_year','سال، ماه و روز تولد را کامل وارد کنید.');bad=bad||'birth_year';}
  if(bad){document.getElementById(bad).focus();return;}
  await withBtn($('#loginBtn'),'در حال بررسی…',async()=>{
    const birth_date=[y,m.padStart(2,'0'),d.padStart(2,'0')].join('/');
    const r=await api('/api/portal/login',{method:'POST',body:{
      national_id:$('#national_code').value.trim(), birth_date:birth_date}});
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
    <label for="note">یادداشت (اختیاری)</label>
    <p class="hint">یادداشت ثبت‌نام را در صورت نیاز مرور یا تکمیل کنید.</p>
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
const REGISTER_NOTE=sessionStorage.getItem('asa_booking_note')||'';

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
      code.textContent='کد '+a.public_id; d.appendChild(code);
      if(a.status==='booked'){
        const cancel=document.createElement('button');
        cancel.type='button'; cancel.className='btn btn-ghost mini cancel-appt';
        cancel.dataset.cancelCode=a.public_id; cancel.textContent='لغو نوبت';
        d.appendChild(cancel);
      }
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
  $('#note').value=REGISTER_NOTE;
  $('#noteWrap').classList.remove('hidden');
  $('#confirmWrap').classList.remove('hidden');
  $('#confirmWrap').scrollIntoView({behavior:'smooth',block:'nearest'});
}

document.addEventListener('click',async e=>{
  const cancel=e.target.closest&&e.target.closest('.cancel-appt');
  if(cancel){
    if(!confirm('این نوبت لغو شود؟')) return;
    cancel.disabled=true;
    const r=await api('/api/portal/appointments/'+encodeURIComponent(cancel.dataset.cancelCode)+'/cancel',{method:'POST'});
    if(!r.ok){cancel.disabled=false;flash(r.error.message||'لغو نوبت انجام نشد.',true);return;}
    flash('نوبت لغو شد.');
    setTimeout(()=>location.reload(),500);
    return;
  }
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
    sessionStorage.removeItem('asa_booking_note');
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

# ==========================================================================
# 6 — messages: the patient side of the doctor <-> patient channel
# ==========================================================================
MESSAGES_BODY = """
<div class="pcard">
  <h1 class="ptitle">پیام‌های شما و کلینیک</h1>
  <p class="psub">
    اینجا مستقیم با پزشک در ارتباطید. تغییر زمان نوبت، خلاصه‌ی جلسات و
    برنامه‌ی درمان هم همین‌جا اطلاع‌رسانی می‌شود.
  </p>

  <div id="mLoading"><div class="skel"></div><div class="skel"></div></div>
  <div id="mError" class="state error hidden"></div>
  <div id="mEmpty" class="state empty hidden">
    هنوز پیامی ردوبدل نشده است.
    <span class="sub">اولین پیام را همین پایین بنویسید؛ پزشک در پنل خودش می‌بیند.</span>
  </div>
  <div class="chat hidden" id="mList"></div>

  <div class="composer">
    <div class="field" style="flex:1;margin:0">
      <textarea id="msgText" placeholder=" " maxlength="1000" rows="2"></textarea>
      <label for="msgText">پیام شما برای پزشک…</label>
    </div>
    <button class="btn btn-primary" id="msgSend" style="padding:13px 20px;flex:0 0 auto">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>
      ارسال
    </button>
  </div>
</div>

<div class="pcard" id="planCard" style="display:none">
  <h2 class="ptitle" style="font-size:1.05rem">برنامه‌ی درمان شما</h2>
  <p class="psub" style="margin-bottom:14px">آنچه پزشک در جلسات گذشته برای شما ثبت کرده است.</p>
  <div class="plan" id="planList"></div>
</div>

<a class="backlink" href="/booking/reserve">
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 18l6-6-6-6"/></svg>
  بازگشت به نوبت‌ها
</a>
"""

MESSAGES_JS = r"""
document.addEventListener('who',e=>{
  if(!e.detail){ location.href='/booking/login'; return; }
  loadThread(); loadPlan();
});

function when(iso){
  try{ return new Date(iso).toLocaleString('fa-IR',{month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
  catch(e){ return ''; }
}

async function loadThread(){
  $('#mLoading').classList.remove('hidden');
  $('#mError').classList.add('hidden');
  const r=await api('/api/portal/messages');
  $('#mLoading').classList.add('hidden');
  if(!r.ok) return errBox('#mError',r.error,loadThread);
  const list=r.data.messages||[];
  const box=$('#mList'); box.innerHTML='';
  if(!list.length){ $('#mEmpty').classList.remove('hidden'); box.classList.add('hidden'); return; }
  $('#mEmpty').classList.add('hidden');
  list.forEach(m=>{
    const d=document.createElement('div');
    d.className='bubble '+(m.sender==='patient'?'me':m.sender==='system'?'sys':'them');
    d.append(document.createTextNode(m.body));
    const w=document.createElement('span'); w.className='when';
    w.textContent=(m.sender==='staff'?'کلینیک · ':m.sender==='system'?'':'شما · ')+when(m.created_at);
    d.appendChild(w);
    box.appendChild(d);
  });
  box.classList.remove('hidden');
  box.scrollTop=box.scrollHeight;
}

async function loadPlan(){
  const r=await api('/api/portal/history');
  if(!r.ok) return;
  const list=(r.data.history||[]).filter(h=>h.plan||h.treatments.length);
  if(!list.length) return;
  $('#planCard').style.display='';
  const box=$('#planList'); box.innerHTML='';
  list.forEach(h=>{
    const d=document.createElement('div'); d.className='rowp';
    const b=document.createElement('b'); b.textContent=h.label; d.appendChild(b);
    if(h.treatments.length){
      const tags=document.createElement('div'); tags.className='tags';
      h.treatments.forEach(t=>{const x=document.createElement('span');x.className='tag';x.textContent=t;tags.appendChild(x);});
      d.appendChild(tags);
    }
    if(h.plan){
      const p=document.createElement('div'); p.style.marginTop='6px';
      p.textContent='برنامه‌ی بعد: '+h.plan; d.appendChild(p);
    }
    box.appendChild(d);
  });
}

$('#msgSend').addEventListener('click',async function(){
  const text=$('#msgText').value.trim();
  if(text.length<2){flash('متن پیام را بنویسید.',true);return;}
  await withBtn(this,'…',async()=>{
    const r=await api('/api/portal/messages',{method:'POST',body:{body:text}});
    if(!r.ok) return flash((r.error.message||'ارسال نشد.'),true);
    $('#msgText').value='';
    flash('پیام شما به کلینیک رسید ✓');
    loadThread();
  });
});
$('#msgText').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('#msgSend').click();}
});
"""

PAGES: dict[str, tuple[str, str, str]] = {
    "entry": ("رزرو نوبت", ENTRY_BODY, ENTRY_JS),
    "register": ("ثبت‌نام بیمار", REGISTER_BODY, REGISTER_JS),
    "login": ("ورود بیماران قبلی", LOGIN_BODY, LOGIN_JS),
    "reserve": ("انتخاب نوبت", RESERVE_BODY, RESERVE_JS),
    "done": ("نوبت ثبت شد", DONE_BODY, DONE_JS),
    "messages": ("پیام‌های من", MESSAGES_BODY, MESSAGES_JS),
}


def page(name: str) -> str:
    title, body, script = PAGES[name]
    return render(title, body, script)
