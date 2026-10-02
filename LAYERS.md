# ۱۴ لایه — گزارش اجرا با شواهد

همه‌ی خروجی‌های زیر از اجرای **واقعی** روی همین سامانه گرفته شده‌اند، نه نمونه.
بازتولید: `./work/verify_layers.sh` (سرویس باید بالا باشد) و `make test`.

> **قالب و محتوای سایت تغییر نکرد.** تنها تفاوت قابل‌مشاهده برای بازدیدکننده،
> رفتار دکمه‌ی «ارسال درخواست نوبت» است که حالا حالت «در حال ارسال» و حالت خطا دارد.
> متن‌ها، رنگ‌ها، چیدمان، تصاویر و هفت صفحه دقیقاً همان‌اند.

---

## ۰۱ · فرانت‌اند ✅

**چهار حالت در فرم رزرو نوبت:**

| حالت | پیاده‌سازی | اثبات |
|---|---|---|
| در حال بارگذاری | دکمه قفل می‌شود، `aria-busy="true"`، اسپینر CSS، متن «در حال ارسال…» | `work/e2e_loading.py` → `{'loading': True, 'busy': 'true', 'disabled': True, 'spinner': 'btnspin'}` |
| خطا | پیام زیر هر فیلد + توست قرمز با کد پیگیری؛ خطای شبکه شماره تلفن را پیشنهاد می‌دهد | `work/e2e.py` → «ارسال نشد؛ اتصال اینترنت را بررسی کنید یا تماس بگیرید: 0902 46 48 159» |
| خالی | در پنل: «هنوز هیچ درخواستی ثبت نشده است» + توضیح اینکه کِی پر می‌شود | `work/e2e.py` → `[admin state: empty] True` |
| موفق | توست سبز + ریست فرم — **فقط وقتی کلینیک واقعاً دریافت کرده باشد** | `form_reset=True`, `delivered: true` |

**۳۷۵ پیکسل:** هر هفت صفحه‌ی سایت و خود پنل، بدون اسکرول افقی.

```
index  False · services False · treatments False · about False
gallery False · faq      False · contact    False · admin False
```

**متن‌ها از دید کاربر:** «شماره موبایل باید ۱۱ رقم و با ۰۹ شروع شود.» —
نه `ValidationError: pattern mismatch`.

---

## ۰۲ · API و منطق بک‌اند ✅

**اعتبارسنجی در مرز** — `api/schemas.py`، Pydantic با `extra="forbid"`.
ارقام فارسی و `+98` خودکار نرمال می‌شوند، کاراکترهای کنترلی و bidi حذف می‌شوند.

**خطاهای نوع‌دار** — پوشش ثابت، همیشه با `trace_id`:

```json
{"error":{"type":"validation_error","code":"invalid_input",
          "message":"اطلاعات واردشده معتبر نیست.",
          "trace_id":"b0ef3857...","fields":{"name":"…","phone":"…"}}}
```

انواع: `validation_error` · `bad_request` · `unauthorized` · `forbidden` ·
`not_found` · `conflict` · `idempotency_key_reuse` · `payload_too_large` ·
`rate_limited` · `circuit_open` · `budget_exceeded` · `dependency_failed` · `internal_error`

**Idempotency** — `Idempotency-Key` روی `POST /api/bookings` **اجباری** است:

```
created = fc4e385573df
replay same key + same body  -> همان public_id + هدر Idempotent-Replay: true
replay same key + other body -> 409 idempotency_key_reuse
```

`notify.deliver()` هم idempotent است (`UNIQUE(booking_id, provider)`): ارسال دوباره
پیام تکراری نمی‌فرستد.

---

## ۰۳ · پایگاه‌داده ✅

```
tables     : 10
triggers   : none
migrations : ['0001', '0002']
CHECK enforced: CHECK constraint failed: phone GLOB '09[0-9][0-9]...
```

- **محدودیت‌ها صریح‌اند**: هر ستون `NOT NULL`/`DEFAULT`/`CHECK` دارد؛ enumها
  (`status`, `service`, `role`, `kind`, `provider`) در `CHECK` قفل‌اند؛ شکل شماره‌ی
  موبایل با `GLOB` در خود پایگاه‌داده تضمین می‌شود؛ `FOREIGN KEY` روشن است.
- **رو به جلو و نسخه‌دار**: `migrations/0001_init.sql`, `0002_deliveries.sql`.
  هر فایل checksum دارد؛ تغییر یک migration اعمال‌شده باعث **بالا نیامدن برنامه** می‌شود
  (`test_changing_an_applied_migration_is_refused`). CI هم ویرایش/حذف فایل‌های
  `migrations/` را در PR رد می‌کند.
- **هیچ منطقی در trigger**: اصلاً trigger وجود ندارد. اجراکننده‌ی migration
  `CREATE TRIGGER` را رد می‌کند و `assert_no_business_triggers()` در هر بوت بررسی می‌کند.

---

## ۰۴ · احراز هویت و مجوز ✅

**مجوز در لایه‌ی داده است، نه در UI.** تمام دسترسی‌ها از `api/repo.py` می‌گذرند و
`repo` قبل از ساختن هر SQL به `api/policies.py` نگاه می‌کند.

```python
"bookings": TablePolicy(select="staff", insert="system",
                        update="staff", delete="owner")
"admin_users": TablePolicy(select="owner", …, hidden_columns={"password_hash"})
"sessions":    TablePolicy(…, row_filter=_own_sessions)   # کارمند فقط نشست خودش
```

- **هر جدول سیاست صریح دارد** — `test_every_table_has_an_explicit_policy`.
- **fail-closed**: جدول بدون سیاست دسترس‌ناپذیر است، نه باز (`PolicyMissing`).
  تست یک جدول موقت می‌سازد و ثابت می‌کند حتی `SYSTEM` هم نمی‌تواند بخواندش.
- `password_hash` برای هیچ نقش انسانی قابل انتخاب نیست.
- بدون کوکی، خود API رد می‌کند — نه اینکه دکمه را پنهان کند:

```
/api/admin/bookings  -> 401      /api/admin/costs -> 401
/api/admin/metrics   -> 401      /api/admin/ops   -> 401
کارمند روی مسیرهای مدیر -> 403
```

- نوشتن‌های پنل توکن CSRF می‌خواهند (`test_write_requires_csrf`).

---

## ۰۵ · هاستینگ و انتشار ✅

`make deploy ENV=production` — یک دستور، هفت مرحله: preflight → lint و تست →
**پشتیبان‌گیری قبل از تغییر** → ساخت استاتیک → migration → ثبت release → restart و
بررسی `/api/readyz`. Dockerfile و docker-compose برای تکرارپذیری.

**مسیر بازگشت نوشته شده و حداقل یک‌بار استفاده شده** — `docs/ROLLBACK.md`:

```
release 3 با رگرسیون عمدی منتشر شد
  curl /api/healthz → {"marker":"RELEASE-3-REGRESSION", …}
ENVNAME=drill ops/rollback.sh
  rolling back: …e4b85ba -> …4f924e8   ✔ در 3050 ms
  curl /api/healthz → {"ok":true, …}     ← رگرسیون رفت
  grep -c RELEASE-3 api/main.py → 0
```

> تمرین انتشار یک باگ واقعی پیدا کرد: تست‌ها `DB_PATH` را از محیط انتشار به ارث
> می‌بردند و می‌توانستند پایگاه‌داده‌ی واقعی را پاک کنند. برطرف شد (کامیت `4f924e8`)
> و `test_suite_cannot_touch_a_real_db` حالا نگهبانش است.

---

## ۰۶ · کلاد و منابع محاسباتی ✅

جدول در کد است (`api/budget.py::CATALOG`) تا از واقعیت عقب نیفتد:

| منبع | هزینه‌ی واحد | سقف ماهانه | هشدار |
|---|---|---|---|
| `ai_summary` | $0.0012 / فراخوانی | $5.00 | ۸۰٪ |
| `sms` | $0.0045 / پیامک | $3.00 | ۸۰٪ |

- `budget.guard()` **قبل** از خرج‌کردن صدا زده می‌شود → `BudgetExceeded`، پول خرج نمی‌شود.
- `budget.record()` هر خرج را در `cost_events` می‌نویسد.
- هشدار در ۸۰٪ **قبل** از پر شدن سقف می‌رود — `test_alert_fires_before_the_cap`.
- هزینه‌ی زیرساخت وقتی منتشر شود: ≈ $5–6 در ماه → `docs/COSTS.md`.

---

## ۰۷ · CI/CD و کنترل نسخه ✅

مخزن git با ۷ کامیت. `.github/workflows/ci.yml` روی هر `pull_request` به `main`:

```
ruff check → ruff format --check → secret scan → migrations forward-only guard
→ pytest (۶۳ تست) → static build → boot smoke test (/api/readyz)
```

- **شاخه‌ی اصلی همیشه قابل انتشار است**: هیچ job انتشاری بدون سبز شدن `check` اجرا نمی‌شود (`needs: check`).
- **محیط آزمایشی جدا**: job `staging` از `environment: staging` با secretها و
  `DB_PATH` جداگانه استفاده می‌کند — هیچ داده‌ی واقعی مشتری در آن نیست.
- `permissions: contents: read` و `concurrency` برای لغو اجراهای قدیمی.

---

## ۰۸ · امنیت و کنترل دسترسی ✅

```
content-security-policy: default-src 'self'; img-src 'self' data:; font-src 'self' data:;
  style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self';
  form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'
x-frame-options: DENY · x-content-type-options: nosniff
referrer-policy: strict-origin-when-cross-origin
permissions-policy: geolocation=(), microphone=(), camera=(), payment=()
cross-origin-opener-policy: same-origin        (+ HSTS در production)
```

فونت‌ها self-host شدند تا CSP هیچ مبدأ خارجی نخواهد.

- **هیچ کلیدی در مخزن نیست**: فقط `.env.example` ردیابی می‌شود؛ `git ls-files | grep '^\.env' | grep -v example` → خالی. CI اسکن الگو دارد و تستی هم محلی اجرا می‌شود.
- در `APP_ENV=production` نبودن `SECRET_KEY` یعنی برنامه **بالا نمی‌آید**.
- Argon2id، ورود ناموفق با زمان و پیام یکسان برای کاربر موجود و ناموجود، کوکی `HttpOnly`+`SameSite=Strict`.
- **ورودی خصمانه فرض می‌شود**: SQL کاملاً پارامتری، شناسه‌ها سفیدلیست، سقف بدنه ۱۶KB، honeypot، پاک‌سازی کاراکتر کنترلی/bidi، تست تزریق SQL که ثابت می‌کند رشته به‌عنوان **داده** ذخیره می‌شود.

---

## ۰۹ · محدودیت نرخ ✅

| مسیر | per-IP | per-identity | سراسری |
|---|---|---|---|
| هر مسیر `/api` | ۶۰ / دقیقه | — | قطع‌کننده |
| `POST /api/bookings` | ۵ / ساعت | ۳ / روز به ازای **شماره موبایل** | قطع‌کننده |
| `POST /api/admin/login` | ۸ / ۱۵ دقیقه | — | قطع‌کننده |
| `POST /api/outcome` | ۳۰ / ساعت | — | قطع‌کننده |
| **فراخوانی هوش مصنوعی** | از طریق سقف سراسری | ۱۰ / ساعت به ازای کاربر | ۲۰۰ / روز + سقف هزینه |

زنده:
```
burst of 70 requests -> 41 x 429
retry-after: 3549
```

**قطع‌کننده‌ی سراسری**: بیش از ۲۵ خطای ۵xx در ۶۰ ثانیه ⇒ ۳۰ ثانیه `503 circuit_open`
با `Retry-After`. شمارنده‌ها در SQLite‌اند، پس restart آن‌ها را پاک نمی‌کند و بین
workerها مشترک‌اند.

---

## ۱۰ · کش و CDN ✅

```
style.113cd69c52.css   public, max-age=31536000, immutable
assets/*.<hash>.jpg    public, max-age=31536000, immutable
Vazirmatn-*.<hash>.woff2  public, max-age=31536000, immutable
index.html             public, max-age=0, must-revalidate
conditional GET (If-None-Match) -> 304
```

- **درخواست یکسان دوباره محاسبه نمی‌شود**: `cache.memo()` روی خواندن‌های پنل
  + ETag؛ هر نوشتن `cache.purge("admin:bookings")` می‌زند (`test_write_purges_the_cached_read`).
- **فایل‌های ثابت از لبه**: `build_static.py` هر دارایی را به `name.<sha256-10>.ext`
  تبدیل می‌کند. ۱۴ دارایی اثرانگشت‌دار.
- **قانون پاک‌کردن کش نوشته شده** → `docs/CACHE.md`: «هیچ‌وقت purge لازم نیست، چون
  هیچ آدرسی دوبار محتوای متفاوت نمی‌دهد» + دستور دقیق برای تنها استثنا (HTML اشتباه).

---

## ۱۱ · توزیع بار و مقیاس‌پذیری ✅

اندازه‌گیری‌شده، نه حدس (`ops/loadtest.py`، ۴۰۰ درخواست، همزمانی ۲۴):

| مسیر | req/s | p50 | p95 | p99 |
|---|---|---|---|---|
| HTML استاتیک | 345.6 | 63.0 | 101.4 | 117.7 |
| `/api/healthz` | 480.5 | 45.2 | 78.0 | 80.8 |
| `/api/readyz` (خواندن DB) | 550.4 | 41.6 | 54.6 | 60.5 |
| **`POST /api/bookings`** | **252.6** | **82.6** | **149.2** | **164.3** |

> **گلوگاه: نوشتن رزرو نوبت — p95 = ۱۴۹٫۲ms در همزمانی ۲۴.**
> علت ریشه‌ای: قفل نویسنده‌ی سراسری SQLite، که شمارنده‌های محدودیت نرخ هم روی
> همان پایگاه‌داده می‌نویسند.

**قدم بعدی برای ۱۰ برابر** (در `docs/SCALE.md`، با آستانه‌ی مشخص برای هرکدام):
انتقال `rate_events` به Redis → CDN جلوی سایت → Postgres + چند worker →
صف پس‌زمینه برای اطلاع‌رسانی. ظرفیت فعلی حدود ۴۰۰٬۰۰۰ برابر نیاز امروز است،
پس هیچ‌کدام تا رسیدن به آستانه‌اش انجام نمی‌شود.

---

## ۱۲ · ردیابی خطا و لاگ‌ها ✅

```json
{"ts":"2026-10-01T07:13:00.636Z","level":"info","logger":"app","msg":"booking.created",
 "trace_id":"b601334ac5c74e0c","env":"staging","version":"1.0.0",
 "booking_id":1,"delivered":true,"service":"laser"}
```

- **هر خطای مدیریت‌نشده خودکار به کانال هشدار می‌رود** با همان `trace_id` که کاربر
  می‌بیند — `api/tracking.py`، webhook یا `data/alerts.log`، با dedupe پنج‌دقیقه‌ای.
  تست `test_unhandled_exception_alerts_with_a_trace_id` ثابت می‌کند کاربر ۵۰۰ عمومی
  می‌گیرد، جزئیات داخلی درز نمی‌کند، و هشدار با همان کد می‌رسد.
- **لاگ‌ها ساختاریافته و بدون PII**: کلیدهای حساس `[redacted]`، شماره و نام به
  اثرانگشت HMAC (`fp_9bf83145f13b`). جست‌وجوی `0912` در لاگ‌ها → ۰ نتیجه.
- `X-Trace-Id` روی **هر** پاسخ، و در پیام خطای فارسی به کاربر نشان داده می‌شود.

---

## ۱۳ · دسترس‌پذیری و بازیابی ✅

**خودکار**: سرویس `backup` در `docker-compose.yml` هر ۶ ساعت، نگهداری ۱۴ نسخه.

**بازیابی واقعاً تمرین شد** — `docs/RESTORE-DRILL.md`:

```
ops/backup.sh                → snapshot ok, bookings=50   (۸۷ms)
ops/restore.sh … data/drill.db
  restored: 11 tables, 50 bookings, 2 migrations applied
  RTO measured: 38 ms
  integrity: ok · foreign_key_check: بدون تخلف
```

| سنجه | مقدار |
|---|---|
| **RTO اندازه‌گیری‌شده** | **۳۸ میلی‌ثانیه** |
| **RPO** | ۶ ساعت |
| تمرین بعدی | ۱۴۰۴/۱۰/۰۱، روی ماشین تازه |

---

## ۱۴ · سنجش نتیجه ✅

> **عدد اصلی: تعداد درخواست نوبتی که واقعاً به دست کلینیک رسید** (`booking_delivered`).

این عدد فقط وقتی بالا می‌رود که ارائه‌دهنده‌ی پیام‌رسان دریافت را **تأیید** کرده باشد.

- از **migration ۰۰۰۱** جمع‌آوری می‌شود، نه بعداً اضافه شده.
- `booking_submitted` و `delivery_success_rate` به‌عنوان پشتیبان.
- `call_click` و `instagram_click` صراحتاً زیر کلید `vanity` و در پنل با برچسب
  «(تزئینی)» — تا کسی با نتیجه اشتباهشان نگیرد.
- در بالای پنل، در کادر برجسته. `docs/METRIC.md` هدف سه‌ماهه را هم مشخص کرده.

---

## 🔴 ایرادی که ممیزی پیدا کرده بود — برطرف شد

قبلاً فرم می‌گفت «درخواست شما ثبت شد ✓» در حالی که هیچ داده‌ای هیچ‌جا نمی‌رفت.

حالا پیام موفقیت **فقط** وقتی نشان داده می‌شود که کانال کلینیک دریافت را تأیید کرده باشد.
اگر تحویل شکست بخورد، پاسخ `202` با `delivered: false` است، به کاربر گفته می‌شود
تلفنی تماس بگیرد، داده از بین نمی‌رود و در پنل دکمه‌ی «ارسال دوباره به کلینیک» دارد.

`NOTIFY_PROVIDER=file` فقط برای توسعه است — در `APP_ENV=production` برنامه با آن
**بالا نمی‌آید**، تا هرگز در محیط واقعی وانمود نشود نوبتی تحویل شده است.

تست‌های نگهبان: `TestDeliveryHonesty` (سه مورد) + `test_failed_delivery_does_not_count_as_a_result`.
