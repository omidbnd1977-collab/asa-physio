# آسا فیزیو کلینیک — سایت و سامانه‌ی نوبت‌دهی

سایت مرکز فیزیوتراپی آسا فیزیو (جزیره قشم) به‌علاوه‌ی سامانه‌ای که فرم رزرو نوبت را
واقعاً به دست کلینیک می‌رساند.

**قالب و محتوای سایت دست‌نخورده است.** دکمه‌ی «رزرو نوبت» حالا به سامانه‌ی
نوبت‌دهی (`/booking`) می‌رود که با همان تم اقیانوسی ساخته شده است.

| | |
|---|---|
| سایت | `/` — هفت صفحه، بدون تغییر |
| **رزرو نوبت** | `/booking` — ثبت‌نام، ورود بیماران قبلی، جدول نوبت‌ها |
| پنل پزشک | `/admin` — نوبت‌ها، پرونده‌ی بالینی بیماران، پیامک‌ها، سامانه |

ساعت کاری ۱۶:۰۰ تا ۲۲:۰۰، هر نوبت نیم‌ساعت، ۱۰ کابین ⇒ **۱۲ خانه × ۱۰ = ۱۲۰ نوبت در روز**.
جزئیات کامل: `docs/BOOKING.md`.

```bash
make install && cp .env.example .env.staging   # SECRET_KEY، کانال تحویل و SMS را پر کنید
make build && make migrate
ADMIN_USER=owner ADMIN_PASSWORD='...' python3 -m ops.seed
make serve        # سایت: /   ·   پنل کارکنان: /admin
```

---

## وضعیت ۱۴ لایه

| # | لایه | وضعیت | اثبات |
|---|---|---|---|
| ۰۱ | فرانت‌اند | ✅ | چهار حالت در فرم و در پنل · ۳۷۵px بدون اسکرول افقی · `work/e2e.py` |
| ۰۲ | API و منطق بک‌اند | ✅ | `api/schemas.py`، `api/errors.py`، `Idempotency-Key` · `TestLayer02Api` |
| ۰۳ | پایگاه‌داده | ✅ | `migrations/` رو به جلو با checksum · بدون trigger · `TestLayer03Db` |
| ۰۴ | احراز هویت و مجوز | ✅ | `api/policies.py` + `api/repo.py` · `TestLayer04Authz` |
| ۰۵ | هاستینگ و انتشار | ✅ | `ops/deploy.sh` · بازگشت **واقعاً اجرا شد** → `docs/ROLLBACK.md` |
| ۰۶ | کلاد و هزینه | ✅ | `api/budget.py` · `docs/COSTS.md` · `TestLayer06Cost` |
| ۰۷ | CI/CD | ✅ | `.github/workflows/ci.yml` · محیط staging جدا |
| ۰۸ | امنیت | ✅ | `docs/SECURITY.md` · `TestLayer08Security` |
| ۰۹ | محدودیت نرخ | ✅ | `api/ratelimit.py` · per-IP / per-identity / global breaker |
| ۱۰ | کش و CDN | ✅ | `build_static.py` + `api/cache.py` · `docs/CACHE.md` |
| ۱۱ | مقیاس‌پذیری | ✅ | گلوگاه نام‌دار و اندازه‌گیری‌شده → `docs/SCALE.md` |
| ۱۲ | ردیابی خطا و لاگ | ✅ | `api/logging_.py` + `api/tracking.py` · `TestLayer12Observability` |
| ۱۳ | بکاپ و بازیابی | ✅ | بازیابی **واقعاً تمرین شد**، RTO ۳۸ms → `docs/RESTORE-DRILL.md` |
| ۱۴ | سنجش نتیجه | ✅ | `booking_delivered` از migration ۰۰۰۱ → `docs/METRIC.md` |

`make test` → **۱۶۸ تست**. `make lint` → تمیز.

---

## ساختار

```
api/            برنامه‌ی FastAPI
  config.py       تنظیمات از محیط؛ هیچ رمزی در کد
  errors.py       خطاهای نوع‌دار با پوشش ثابت JSON
  schemas.py      اعتبارسنجی مرز (Pydantic)
  db.py           اتصال + اجراکننده‌ی migration رو به جلو
  policies.py     سیاست دسترسی هر جدول  ← لایه ۰۴
  repo.py         تنها راه رسیدن به پایگاه‌داده
  auth.py         Argon2، نشست، CSRF
  ratelimit.py    per-IP، per-identity، قطع‌کننده‌ی سراسری
  budget.py       هزینه‌ی واحد، سقف ماهانه، هشدار پیش از سقف
  notify.py       رساندن نوبت به کلینیک (و اثبات رسیدنش)
  metrics.py      عدد لایه ۱۴
  cache.py        memo + ETag + سیاست Cache-Control
  tracking.py     هشدار خودکار خطاهای مدیریت‌نشده
  logging_.py     لاگ JSON با پاک‌سازی PII
  ai.py           تنها مسیر پولی، با تمام نگهبان‌ها
  jalali.py       تقویم شمسی + اعتبارسنجی کد ملی
  scheduling.py   ساعت کاری، جدول ۱۲×۱۰، تخصیص کابین بدون برخورد
  patients.py     ثبت‌نام، ورود بیمار، نشست، آپلود امن
  sms.py          پیامک بیمار (کاوه‌نگار / sms.ir / وبهوک)
  reminders.py    یادآوری ۲ ساعت قبل + دریافت پاسخ «۱»
  clinical.py     کمبوباکس‌های گسترش‌پذیر، تاریخچه‌ی درمان، قانون بازگشت پس از وقفه
  routes_booking.py  API بیمار
  routes_doctor.py   API پزشک
  pages.py        صفحه‌های سامانه‌ی نوبت‌دهی
  templates/      پنل کارکنان + پوسته‌ی سامانه‌ی نوبت
migrations/     SQL نسخه‌دار، فقط رو به جلو
ops/            deploy · rollback · backup · restore · serve · seed · loadtest
tests/          ۶۳ تست، یکی به ازای هر ادعا
docs/           BOOKING (سامانه‌ی نوبت‌دهی) · RUNBOOK · SECURITY · CACHE
                SCALE · COSTS · METRIC · ROLLBACK و RESTORE-DRILL (هر دو تمرین‌شده)
work/           منبع سایت (template.html) + اسکریپت‌های ساخت و تست مرورگر
site/ public/   خروجی سایت؛ public/ نسخه‌ی اثرانگشت‌دار برای سرو
```

---

## نکته‌ی مهم: پیام موفقیت حالا راست می‌گوید

قبلاً فرم می‌گفت «درخواست شما ثبت شد» ولی هیچ داده‌ای جایی نمی‌رفت.
حالا:

1. درخواست اعتبارسنجی و ذخیره می‌شود،
2. به کانال کلینیک (تلگرام یا وبهوک) فرستاده می‌شود،
3. فقط اگر ارائه‌دهنده **تأیید کند**، پاسخ `delivered: true` و پیام موفقیت می‌آید،
4. اگر نرسد، پاسخ `202` با `delivered: false` است و به کاربر گفته می‌شود تلفنی تماس بگیرد —
   داده از بین نمی‌رود و در پنل دکمه‌ی «ارسال دوباره» دارد.

`NOTIFY_PROVIDER=file` فقط برای توسعه است؛ در `APP_ENV=production` برنامه با آن
**بالا نمی‌آید**، تا هیچ‌وقت در محیط واقعی وانمود نشود که نوبتی تحویل شده است.
