/* helpers مشترک: csrf، درخواست، توست، ماسک ارقام */
const FA = "۰۱۲۳۴۵۶۷۸۹";
const fa = (v) => String(v ?? "").replace(/\d/g, (d) => FA[+d]);
const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

async function api(path, { method = "GET", body = null, form = null } = {}) {
  const opt = { method, headers: { "X-CSRF-Token": csrfToken(), Accept: "application/json" },
                credentials: "same-origin" };
  if (form) opt.body = form;
  else if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch { /* پاسخ متنی */ }
  return { ok: res.ok, status: res.status, data: data ?? {} };
}

let toastTimer;
function toast(msg, bad = false) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.className = "toast on" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 4200);
}

function showMsg(sel, text, kind = "err") {
  const box = document.querySelector(sel);
  if (!box) return;
  box.innerHTML = text ? `<div class="notice ${kind}">${text}</div>` : "";
}

/* ماسک: فقط رقم، و پرش خودکار به کادر بعدی */
function wireMasks(root = document) {
  root.querySelectorAll("input.digits").forEach((inp) => {
    if (inp.dataset.masked) return;
    inp.dataset.masked = "1";
    inp.addEventListener("input", () => {
      const before = inp.value;
      inp.value = before.replace(/[۰-۹٠-٩]/g, (c) => {
        const i = "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩".indexOf(c);
        return String(i % 10);
      }).replace(/\D/g, "").slice(0, +(inp.maxLength || 20) || 20);
      if (inp.maxLength && inp.value.length >= +inp.maxLength && inp.classList.contains("auto-next")) {
        const all = [...inp.form.querySelectorAll("input")];
        const nxt = all[all.indexOf(inp) + 1];
        if (nxt) nxt.focus();
      }
    });
  });
}

/* فرم‌های ajax‌شوندهی ساده (ثبت‌نام/ورود/تماس) */
function wireForm(formSel, boxSel, onOk) {
  const form = document.querySelector(formSel);
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    if (btn) btn.disabled = true;
    showMsg(boxSel, "");
    const isFile = !!form.querySelector('input[type=file]') || form.enctype.includes("multipart");
    const r = await api(form.action, { method: "POST", body: isFile ? new FormData(form) : Object.fromEntries(new FormData(form)) });
    if (btn) btn.disabled = false;
    if (r.ok) { if (onOk) onOk(r); else toast("ثبت شد"); }
    else showMsg(boxSel, r.data.message || `خطا (${r.status})`);
  });
  wireMasks(form);
}

document.addEventListener("DOMContentLoaded", () => {
  wireMasks();
  wireForm("#contact-form", "#contact-out", (r) => {
    showMsg("#contact-out", r.data.message || "درخواست شما ثبت شد.", "ok");
    r.form?.reset?.();
  });
  document.querySelectorAll("form#contact-form").forEach((f) => {
    f.addEventListener("submit", (e) => e.preventDefault());
  });
});
