/* کریستال دنت — تعامل‌های قالب: پارالاکس سه‌بعدی، آشکارسازی اسکرول، شمارنده، فرم نمایشی */
(() => {
  "use strict";

  const FA = "۰۱۲۳۴۵۶۷۸۹";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const clamp = (v, a = 0, b = 1) => Math.min(b, Math.max(a, v));

  const toFa = (n) => String(n).replace(/\d/g, (d) => FA[+d]);
  const group = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, "٬");

  /* ---------- 1. sticky nav + scroll progress ---------- */
  const nav = $("#nav");
  const bar = $("#progressBar");
  const onScroll = () => {
    const y = window.scrollY || 0;
    nav?.classList.toggle("is-scrolled", y > 24);
    if (bar) {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      bar.style.width = `${(max > 0 ? clamp(y / max) : 0) * 100}%`;
    }
    if (tooth) tooth.style.setProperty("--scroll-rot", `${y * 0.03}deg`);
  };

  /* ---------- 2. mobile menu ---------- */
  const burger = $("#burger");
  const menu = $("#mobileMenu");
  const setMenu = (open) => {
    if (!burger || !menu) return;
    burger.setAttribute("aria-expanded", String(open));
    burger.setAttribute("aria-label", open ? "بستن منو" : "باز کردن منو");
    menu.hidden = !open;
  };
  burger?.addEventListener("click", () =>
    setMenu(burger.getAttribute("aria-expanded") !== "true")
  );
  menu?.addEventListener("click", (e) => {
    if (e.target.closest("a")) setMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setMenu(false);
  });

  /* ---------- 3. crystal stage: mouse parallax + scroll spin ---------- */
  const stage = $(".stage");
  const tooth = $("#toothWrap");
  const layers = $$("[data-depth]", stage || document);
  let pointer = { x: 0, y: 0 };
  let raf = 0;

  const paint = () => {
    raf = 0;
    if (tooth) {
      tooth.style.setProperty("--ry", `${pointer.x * 9}deg`);
      tooth.style.setProperty("--rx", `${-pointer.y * 7}deg`);
    }
    layers.forEach((el) => {
      const d = Number(el.dataset.depth) || 10;
      el.style.transform = `translate3d(${(-pointer.x * d).toFixed(2)}px, ${(-pointer.y * d).toFixed(2)}px, 0)`;
    });
  };

  stage?.addEventListener("pointermove", (e) => {
    if (reduce) return;
    const r = stage.getBoundingClientRect();
    pointer = {
      x: clamp((e.clientX - r.left) / r.width, 0, 1) * 2 - 1,
      y: clamp((e.clientY - r.top) / r.height, 0, 1) * 2 - 1,
    };
    if (!raf) raf = requestAnimationFrame(paint);
  });
  stage?.addEventListener("pointerleave", () => {
    pointer = { x: 0, y: 0 };
    if (!raf) raf = requestAnimationFrame(paint);
  });

  /* ---------- 4. cursor light ---------- */
  const spot = $("#spot");
  if (spot && !reduce) {
    let sraf = 0;
    let pos = { x: 0, y: 0 };
    window.addEventListener(
      "pointermove",
      (e) => {
        pos = { x: e.clientX, y: e.clientY };
        if (!sraf) {
          sraf = requestAnimationFrame(() => {
            sraf = 0;
            spot.style.setProperty("--sx", `${pos.x}px`);
            spot.style.setProperty("--sy", `${pos.y}px`);
          });
        }
      },
      { passive: true }
    );
  }

  /* ---------- 5. reveal on scroll ---------- */
  const revealables = $$(".reveal");
  if (reduce || !("IntersectionObserver" in window)) {
    revealables.forEach((el) => el.classList.add("in"));
  } else {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add("in");
          io.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.12 }
    );
    revealables.forEach((el) => io.observe(el));
  }

  /* ---------- 6. counters ---------- */
  const counters = $$("[data-count]");
  const runCount = (el) => {
    const target = Number(el.dataset.count) || 0;
    const suffix = el.dataset.suffix || "";
    if (reduce) {
      el.textContent = toFa(group(target)) + suffix;
      return;
    }
    const dur = 1500;
    const start = performance.now();
    const tick = (now) => {
      const p = clamp((now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = toFa(group(Math.round(target * eased))) + suffix;
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  };
  if (counters.length) {
    if ("IntersectionObserver" in window) {
      const cio = new IntersectionObserver(
        (entries) => {
          entries.forEach((entry) => {
            if (!entry.isIntersecting) return;
            runCount(entry.target);
            cio.unobserve(entry.target);
          });
        },
        { threshold: 0.5 }
      );
      counters.forEach((el) => cio.observe(el));
    } else {
      counters.forEach(runCount);
    }
  }

  /* ---------- 7. service cards: 3D tilt + light follow ---------- */
  $$(".svc").forEach((card) => {
    if (reduce) return;
    card.addEventListener("pointermove", (e) => {
      const r = card.getBoundingClientRect();
      const nx = (e.clientX - r.left) / r.width;
      const ny = (e.clientY - r.top) / r.height;
      card.style.setProperty("--tx", `${(nx - 0.5) * 7}deg`);
      card.style.setProperty("--ty", `${(0.5 - ny) * 6}deg`);
      card.style.setProperty("--gx", `${nx * 100}%`);
      card.style.setProperty("--gy", `${ny * 100}%`);
    });
    card.addEventListener("pointerleave", () => {
      card.style.setProperty("--tx", "0deg");
      card.style.setProperty("--ty", "0deg");
    });
  });

  /* ---------- 8. demo booking form ---------- */
  const form = $("#bookingForm");
  const toast = $("#toast");
  const note = $("#formNote");
  let toastTimer;

  const say = (msg) => {
    if (!toast) return;
    toast.textContent = msg;
    toast.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("on"), 4600);
  };

  const faDigits = (v) => v.replace(/[۰-۹]/g, (d) => String(FA.indexOf(d)));

  form?.addEventListener("submit", (e) => {
    e.preventDefault();
    const name = $("#f-name");
    const phone = $("#f-phone");
    const digits = faDigits(phone?.value || "").replace(/[\s-]/g, "");
    let ok = true;

    const mark = (input, valid) => {
      if (!input) return;
      input.setAttribute("aria-invalid", String(!valid));
      if (!valid) ok = false;
    };

    mark(name, (name?.value || "").trim().length >= 3);
    mark(phone, /^0\d{10}$/.test(digits));

    if (!ok) {
      say("لطفاً نام و شمارهٔ تماس را کامل وارد کنید.");
      return;
    }
    if (note) {
      note.textContent = "این قالب نمایشی است؛ درخواست شما ارسال نشد. پس از اتصال سامانهٔ نوبت‌دهی، نوبت‌ها ثبت می‌شوند.";
    }
    form.reset();
    [name, phone].forEach((i) => i?.removeAttribute("aria-invalid"));
    say("قالب نمایشی است — برای نوبت واقعی با کلینیک تماس بگیرید.");
  });

  /* ---------- 9. footer year (Jalali) ---------- */
  try {
    const jy = new Intl.DateTimeFormat("fa-IR", { year: "numeric" }).format(new Date());
    const year = $("#year");
    if (year) year.textContent = jy;
  } catch (_) {
    /* تقویم شمسی در همهٔ مرورگرها نیست — مقدار پیش‌فرض می‌ماند */
  }

  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();
})();
