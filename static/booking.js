/* جدول نوبت‌ها: نوار ۳۰ روزه، خانه‌های ساعت، رزرو، لغو */
(() => {
  let sel = document.querySelector(".day.sel")?.dataset.date;
  let picked = null;
  const slotsBox = document.getElementById("slots");
  const msg = "#grid-msg";
  const btn = document.getElementById("do-book");
  const note = document.getElementById("note");
  const capNote = document.getElementById("grid-note");
  const idem = (window.crypto?.randomUUID?.() ?? String(Math.random())).slice(0, 30);

  function render(grid) {
    slotsBox.innerHTML = "";
    grid.slots.forEach((s) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "slot" + (s.few ? " few" : "");
      b.dataset.time = s.time;
      if (s.disabled) b.disabled = true;
      const label = s.reason === "past" ? "گذشته" : s.reason === "full" ? "پر" : fa(s.free) + " جای خالی";
      b.innerHTML = `<span class="t">${fa(s.time)}</span><span class="f">${label}</span>`;
      b.title = s.disabled ? "قابل انتخاب نیست" : `${s.free} جای خالی از ${s.capacity}`;
      b.addEventListener("click", () => pick(b));
      slotsBox.appendChild(b);
    });
    picked = null;
    btn.disabled = true;
    btn.textContent = "رزرو این ساعت";
    const open = grid.slots.filter((s) => !s.disabled).length;
    capNote.textContent = `${open} خانه‌ی باز از ${fa(grid.slots.length)} · ظرفیت روز ${fa(grid.capacity_per_day)}`;
  }

  function pick(b) {
    slotsBox.querySelectorAll(".picked").forEach((x) => x.classList.remove("picked"));
    b.classList.add("picked");
    picked = b.dataset.time;
    btn.disabled = false;
    btn.textContent = "رزرو " + fa(picked);
    showMsg(msg, "");
  }

  async function load(date) {
    sel = date;
    const r = await api("/api/slots?date=" + date);
    if (!r.ok) return toast(r.data.message || "خطا", true);
    render(r.data.grid);
    document.querySelectorAll(".day").forEach((d) => {
      const on = d.dataset.date === date;
      d.classList.toggle("sel", on);
      d.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  document.querySelectorAll(".day").forEach((d) => d.addEventListener("click", () => load(d.dataset.date)));

  btn?.addEventListener("click", async () => {
    if (!picked) return;
    btn.disabled = true;
    const r = await api("/api/reserve", {
      method: "POST",
      body: { date: sel, time: picked, note: note?.value || "", idempotency_key: idem },
    });
    btn.disabled = false;
    if (r.ok) {
      const code = r.data.appointment?.tracking_code || "";
      window.location.href = "/booking/done?code=" + encodeURIComponent(code);
      return;
    }
    showMsg(msg, r.data.message || `خطا (${r.status})`, "err");
    if (r.data.error === "slot_full" && r.data.grid) render(r.data.grid);   // جدول بی‌درنگ تازه می‌شود
    if (r.status === 429) toast("سقف روزانه پر شده", true);
  });

  document.querySelectorAll("[data-cancel]").forEach((b) => {
    b.addEventListener("click", async () => {
      if (!confirm("این نوبت لغو شود؟ پیامک لغو هم می‌رود.")) return;
      const r = await api("/api/appointments/cancel", { method: "POST", body: { id: +b.dataset.cancel } });
      if (r.ok) { b.closest(".appt-row").style.opacity = ".45"; b.remove(); toast("نوبت لغو شد"); }
      else toast(r.data.message || "خطا", true);
    });
  });

  /* خانه‌های ilk را همان‌طور که سرور ساخته نگه می‌داریم؛ فقط کلیک وصل می‌کنیم */
  function attach() {
    slotsBox.querySelectorAll(".slot").forEach((el) => el.addEventListener("click", () => pick(el)));
  }
  attach();
})();
