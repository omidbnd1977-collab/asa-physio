/* پنل پزشک: تب‌ها، جدول ۱۲×کابین، جلسه‌ی درمان، فایل‌های بالینی، وبهوک */
(() => {
  const CSRF = csrfToken();
  const BODY = JSON.parse(document.getElementById("data-body-parts")?.textContent || "[]");
  const TRT = JSON.parse(document.getElementById("data-treatments")?.textContent || "[]");

  /* ---------------- تب‌ها ---------------- */
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("on", x === t));
    document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("on", p.id === "pane-" + t.dataset.tab));
  }));

  /* ---------------- مودال‌ها ---------------- */
  const open = (id) => document.getElementById(id).classList.add("on");
  const close = (id) => document.getElementById(id).classList.remove("on");
  document.querySelectorAll("[data-close]").forEach((b) =>
    b.addEventListener("click", () => b.closest(".modal").classList.remove("on")));
  document.querySelectorAll(".modal").forEach((m) =>
    m.addEventListener("click", (e) => { if (e.target === m) m.classList.remove("on"); }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") document.querySelectorAll(".modal.on").forEach((m) => m.classList.remove("on"));
  });

  /* ---------------- جدول ۱۲×کابین (تغییر زمان / جلسه بعدی / نوبت تازه) ---------------- */
  let mode = null, targetId = null, picked = { date: null, time: null };
  const daySel = document.getElementById("picker-day");
  const slotsBox = document.getElementById("picker-slots");
  const okBtn = document.getElementById("picker-ok");
  let grids = [];

  function paintPicker() {
    const g = grids.find((x) => x.jdate === daySel.value) || grids[0];
    picked.date = g.jdate;
    slotsBox.innerHTML = "";
    document.getElementById("picker-cap").textContent =
      `ظرفیت امروز ${fa(g.capacity_per_day)} · ${fa(g.slots.filter((s) => !s.disabled).length)} خانه باز`;
    g.slots.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "slot" + (s.few ? " few" : "");
      if (s.disabled) b.disabled = true;
      const label = s.reason === "past" ? "گذشته" : s.reason === "full" ? "پر" : fa(s.free) + " جا";
      b.innerHTML = `<span class="t">${fa(s.time)}</span><span class="f">${label}</span>`;
      b.addEventListener("click", () => {
        slotsBox.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
        b.classList.add("picked");
        picked.time = s.time;
        okBtn.disabled = false;
        showMsg("#picker-msg", "");
      });
      slotsBox.appendChild(b);
    });
    okBtn.disabled = true;
    picked.time = null;
  }

  daySel?.addEventListener("change", paintPicker);

  async function openPicker(spec) {
    const [m, id, day] = spec.split(":");
    mode = m; targetId = id === "new" ? null : +id;
    const today = day || document.querySelector(".day-h b")?.dataset?.jdate;
    document.getElementById("picker-title").textContent =
      m === "move" ? "تغییر زمان نوبت" : m === "followup" ? "ثبت جلسه‌ی بعدی" : "نوبت تازه";
    document.getElementById("picker-day").innerHTML = "";
    const r = await api("/api/admin/slot-grid?date=" + encodeURIComponent(today) + "&days=14");
    grids = r.data.grids || [];
    if (!grids.length) return toast("جدولی پیدا نشد", true);
    grids.forEach((g) => {
      const o = document.createElement("option");
      o.value = g.jdate;
      o.textContent = `${g.day.weekday_name} ${g.day.slash}${g.jdate === today ? " · انتخابی" : ""}`;
      daySel.appendChild(o);
    });
    daySel.value = today || grids[0].jdate;
    paintPicker();
    showMsg("#picker-msg", "");
    open("picker");
  }

  document.querySelectorAll("[data-open-picker]").forEach((b) =>
    b.addEventListener("click", () => openPicker(b.dataset.openPicker)));

  okBtn?.addEventListener("click", async () => {
    if (!picked.date || !picked.time) return;
    const body = { date: picked.date, time: picked.time, note: document.getElementById("picker-note").value };
    let path;
    if (mode === "move") path = `/api/admin/appointments/${targetId}/reschedule`;
    else if (mode === "followup") path = `/api/admin/appointments/${targetId}/followup`;
    else { path = "/api/admin/appointments/new"; body.patient_id = 0; }
    const r = await api(path, { method: "POST", body });
    if (r.ok) { toast(mode === "move" ? "زمان عوض شد و پیامک رفت" : "ثبت شد و پیامک رفت");
      close("picker"); setTimeout(() => location.reload(), 700); return; }
    const extra = r.data.error === "slot_full" ? " — نوبت قبلی دست‌نخورده ماند." : "";
    showMsg("#picker-msg", (r.data.message || "خطا") + extra);
    if (r.data.grid) { grids = grids.map((g) => (g.jdate === picked.date ? r.data.grid : g)); paintPicker(); }
    if (r.data.original) console.info("original intact:", r.data.original.slot_date, r.data.original.slot_time);
  });

  /* ---------------- کارهای روی نوبت ---------------- */
  async function setStatus(id, status) {
    const r = await api(`/api/admin/appointments/${id}/status`, { method: "POST", body: { status } });
    if (r.ok) { toast("وضعیت ثبت شد"); setTimeout(() => location.reload(), 500); }
    else toast(r.data.message || "خطا", true);
  }
  document.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () =>
    setStatus(b.dataset.id, b.dataset.act)));

  document.querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("نوبت لغو و کابین آزاد شود؟ پیامک لغو هم می‌رود.")) return;
    const r = await api(`/api/admin/appointments/${b.dataset.cancel}/status`,
      { method: "POST", body: { status: "cancelled", sms: "1" } });
    if (r.ok) { toast("لغو شد و پیامک رفت"); setTimeout(() => location.reload(), 600); }
    else toast(r.data.message || "خطا", true);
  }));

  document.querySelectorAll("[data-remind]").forEach((b) => b.addEventListener("click", async () => {
    const r = await api("/api/admin/reminder", { method: "POST", body: { id: +b.dataset.remind } });
    toast(r.ok ? (r.data.status === "sent" ? "یادآوری رفت" : "ارسال نشد: " + r.data.error) : "خطا", !r.ok);
  }));

  /* ---------------- بارگذاری MRI ---------------- */
  document.querySelectorAll("[data-upload-mri]").forEach((inp) => inp.addEventListener("change", async () => {
    const f = inp.files[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("mri", f);
    fd.append("csrf_token", CSRF);
    const r = await api(`/api/admin/patients/${inp.dataset.uploadMri}/mri`, { method: "POST", form: fd });
    toast(r.ok ? "فایل MRI ذخیره شد" : (r.data.message || "خطا"), !r.ok);
    if (r.ok) setTimeout(() => location.reload(), 600);
  }));

  /* ---------------- نورچین فایل بالینی (از مسیر کارکنان) ---------------- */
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => {
    const kind = b.dataset.view;
    const url = `/api/admin/patients/${b.dataset.pid}/${kind === "mri" ? "mri" : "photo"}`;
    const body = document.getElementById("viewer-body");
    document.getElementById("viewer-title").textContent = kind === "mri" ? "فایل MRI" : "عکس داروها";
    body.innerHTML = `<img alt="" src="${url}"><div class="tiny muted" style="padding:8px 4px">
      فقط برای کارکنان · بدون مسیر عمومی · <code>Cache-Control: private, no-store</code>
      <a href="${url}" target="_blank" rel="noopener" style="margin-inline-start:8px">باز کردن در تب</a></div>`;
    const img = body.querySelector("img");
    img.addEventListener("error", () => {
      body.innerHTML = `<div class="notice warn">فایلی برای این بیمار ثبت نشده —
        پزشک می‌تواند از ردیف نوبت بارگذاری کند.</div>`;
    });
    open("viewer");
  }));

  /* ---------------- جست‌وجوی بیماران ---------------- */
  const q = document.getElementById("pat-q");
  const count = document.getElementById("pat-count");
  const cards = () => [...document.querySelectorAll("#pat-list [data-search]")];
  function search() {
    const term = (q.value || "").trim();
    let n = 0;
    cards().forEach((c) => {
      const hit = !term || c.dataset.search.toLowerCase().includes(term.toLowerCase());
      c.style.display = hit ? "" : "none";
      if (hit) n++;
    });
    count.textContent = term ? `${fa(n)} نتیجه` : "";
  }
  q?.addEventListener("input", search);

  /* ---------------- یادداشت پزشک ---------------- */
  document.querySelectorAll("[data-note]").forEach((ta) => {
    ta.addEventListener("blur", async () => {
      const r = await api(`/api/admin/patients/${ta.dataset.note}/note`,
        { method: "POST", body: { note: ta.value } });
      if (r.ok) toast("یادداشت ذخیره شد");
    });
  });

  /* ---------------- پرونده و تاریخچه ---------------- */
  document.querySelectorAll("[data-history]").forEach((b) => b.addEventListener("click", async () => {
    const r = await api("/api/admin/patients/" + b.dataset.history);
    if (!r.ok) return toast("خطا", true);
    const p = r.data.patient;
    document.getElementById("profile-name").textContent = "پرونده — " + b.dataset.name;
    const tl = (r.data.timeline || []).map((s) => `
      <li>
        <b>${s.date.long}</b>
        ${s.gap_days ? `<span class="tag warn">پس از ${fa(s.gap_days)} روز وقفه</span>` : ""}
        <div class="tiny">${s.parts ? "نواحی: " + s.parts : ""}</div>
        <div class="tiny">${s.treatments ? "درمان‌ها: " + s.treatments : ""}</div>
        ${s.pain_before != null ? `<div class="pain"><span class="b">درد ${fa(s.pain_before)}</span> →
          <span class="a">${fa(s.pain_after)}</span></div>` : ""}
        ${s.findings ? `<div class="tiny muted">یافته: ${s.findings}</div>` : ""}
        ${s.next_plan ? `<div class="tiny muted">برنامه: ${s.next_plan}</div>` : ""}
      </li>`).join("");
    const ap = (r.data.appointments || []).map((a) =>
      `<tr><td class="nowrap">${a.day.medium} ${fa(a.day.jd)}/${fa(a.day.jm)}</td>
       <td class="nowrap">${a.time_fa}</td><td>${a.cabin_fa}</td>
       <td><span class="tag ${a.status}">${a.status}</span></td>
       <td class="tiny mono">${a.tracking_code}</td></tr>`).join("");
    document.getElementById("profile-body").innerHTML = `
      <dl class="kv">
        <dt>تاریخ تولد</dt><dd>${p.birth.slash}</dd>
        <dt>موبایل</dt><dd class="mono tiny">${p.phone_disp}</dd>
        <dt>کد ملی</dt><dd class="mono tiny">${p.nc_fp}</dd>
        ${p.note ? `<dt>یادداشت</dt><dd>${p.note}</dd>` : ""}
      </dl>
      <h4>نوبت‌ها</h4>
      <div class="table-wrap"><table><tbody>${ap || '<tr><td class="muted tiny">نوبتی نیست</td></tr>'}</tbody></table></div>
      <h4 style="margin-top:14px">تاریخچه‌ی درمان</h4>
      <ul class="timeline">${tl || '<li class="tiny muted">جلسه‌ای ثبت نشده.</li>'}</ul>
      <div class="tiny muted" style="margin-top:10px">این داده بالینی است و در پورتال بیمار نمایش داده نمی‌شود.</div>`;
    open("profile");
  }));

  /* ---------------- جلسه‌ی درمان ---------------- */
  const pickedParts = new Set(), pickedTrt = new Set();
  function chips(box, groups, set, single = false) {
    box.innerHTML = "";
    const paint = (items, group) => {
      if (group) {
        const l = document.createElement("span");
        l.className = "group-lbl"; l.textContent = group;
        box.appendChild(l);
      }
      items.forEach((it) => {
        const c = document.createElement("button");
        c.type = "button";
        c.className = "chip" + (set.has(it.label) ? " on" : "");
        c.innerHTML = it.label + (it.used_count ? ` <span class="g">${fa(it.used_count)}</span>` : "");
        c.addEventListener("click", () => {
          if (single) set.clear();
          set.has(it.label) ? set.delete(it.label) : set.add(it.label);
          if (single) set.add(it.label);
          paint2();
        });
        box.appendChild(c);
      });
    };
    const paint2 = () => {
      box.innerHTML = "";
      if (Array.isArray(groups[0])) groups[0].forEach(paint);
      else paint(groups);
      box.querySelectorAll(".chip").forEach((el) =>
        el.classList.toggle("on", set.has(el.textContent.replace(/\s*\d[\d٠-٩]*\s*$/, "").trim())));
    };
    if (Array.isArray(groups[0])) groups[0].forEach(paint); else paint(groups);
  }

  const sPartBox = document.getElementById("parts");
  const sTrtBox = document.getElementById("treats");
  let sessionId = null;

  document.querySelectorAll("[data-session]").forEach((b) => b.addEventListener("click", () => {
    sessionId = { appt: +b.dataset.session, pid: +b.dataset.pid };
    document.getElementById("session-name").textContent = b.dataset.name;
    pickedParts.clear(); pickedTrt.clear();
    chips(sPartBox, BODY, pickedParts);
    chips(sTrtBox, TRT, pickedTrt);
    ["pain-before", "pain-after", "findings", "next-plan"].forEach((id) =>
      document.getElementById(id).value = "");
    document.getElementById("notify").checked = false;
    showMsg("#session-msg", "");
    wireMasks(document.getElementById("session"));
    open("session");
  }));

  document.querySelectorAll("[data-add]").forEach((b) => b.addEventListener("click", async () => {
    const isPart = b.dataset.add === "part";
    const inp = document.getElementById(isPart ? "add-part" : "add-treat");
    const label = (inp.value || "").trim();
    if (label.length < 2) return toast("نام را کامل بنویسید", true);
    const r = await api("/api/admin/items", { method: "POST", body: { kind: isPart ? "part" : "treat", label } });
    if (!r.ok) return toast(r.data.message || "خطا", true);
    toast("به فهرست اضافه شد — از این لحظه برای همه هست");
    inp.value = "";
    if (isPart) { BODY.forEach((g) => g.items.push({ label, used_count: 0 })); chips(sPartBox, BODY, pickedParts); }
    else { TRT.push({ label, used_count: 0 }); chips(sTrtBox, TRT, pickedTrt); }
    pickedParts.add(label); pickedTrt.add(label);
  }));

  document.getElementById("session-ok")?.addEventListener("click", async () => {
    if (!sessionId) return;
    const body = {
      patient_id: sessionId.pid, appointment_id: sessionId.appt,
      parts: [...pickedParts].join("|"), treatments: [...pickedTrt].join("|"),
      pain_before: document.getElementById("pain-before").value,
      pain_after: document.getElementById("pain-after").value,
      findings: document.getElementById("findings").value,
      next_plan: document.getElementById("next-plan").value,
      notify: document.getElementById("notify").checked ? "1" : "",
    };
    const r = await api("/api/admin/sessions", { method: "POST", body });
    if (!r.ok) return showMsg("#session-msg", r.data.message || "خطا");
    close("session");
    toast("جلسه ثبت شد · نوبت «انجام شد» · شمارنده بالا رفت");
    setTimeout(() => location.reload(), 800);
  });

  /* ---------------- درخواست‌های سایت ---------------- */
  document.querySelectorAll("[data-handle]").forEach((b) => b.addEventListener("click", async () => {
    await api(`/api/admin/requests/${b.dataset.handle}/handle`, { method: "POST", body: {} });
    setTimeout(() => location.reload(), 300);
  }));

  /* ---------------- شبیه‌ساز پاسخ پیامک ---------------- */
  const secretBox = document.getElementById("in-secret");
  if (secretBox) secretBox.placeholder = "از SMS_INBOUND_SECRET پر کنید تا ۴۰۳ نگیرید";
  document.getElementById("in-send")?.addEventListener("click", () => post(""));
  document.getElementById("in-bad")?.addEventListener("click", () => post("wrong-secret"));
  async function post(force) {
    const res = await fetch("/api/sms/inbound", {
      method: "POST",
      headers: { "Content-Type": "application/json",
                 "X-SMS-Secret": force !== "" ? force : (secretBox.value || "") },
      body: JSON.stringify({ phone: document.getElementById("in-phone").value,
                             body: document.getElementById("in-body").value }),
    });
    const data = await res.json().catch(() => ({}));
    document.getElementById("in-out").textContent =
      `HTTP ${res.status}\n` + JSON.stringify(data, null, 1);
    if (res.ok) toast("ثبت شد — اگر «۱» بود، نزدیک‌ترین نوبت «حضور قطعی» می‌شود");
    setTimeout(() => location.reload(), 900);
  }
})();
