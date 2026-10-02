# لایه ۰۸ — امنیت

## اسرار
- هیچ کلید یا رمزی در مخزن نیست. فقط `.env.example` ردیابی می‌شود.
- `.gitignore` تمام `.env.*` را جز نمونه حذف می‌کند.
- در محیط production نبودن `SECRET_KEY` باعث **بالا نیامدن برنامه** می‌شود، نه هشدار.
- CI اسکن الگوی کلید (`sk-…`, `AKIA…`, `PRIVATE KEY`) دارد؛ تست `test_no_secret_is_committed` همان را محلی اجرا می‌کند.

## گذرواژه و نشست
- Argon2id (`time_cost=3`, `memory_cost=64MB`, `parallelism=2`)، rehash خودکار.
- ورود ناموفق برای کاربر موجود و ناموجود **زمان و پیام یکسان** دارد (هش ساختگی واقعی).
- کوکی نشست: `HttpOnly`، `SameSite=Strict`، `Secure` در production، عمر ۱۲ ساعت.
- تمام نوشتن‌های پنل توکن CSRF می‌خواهند.

## هدرها
`Content-Security-Policy` بدون هیچ مبدأ خارجی (فونت‌ها self-host شده‌اند)،
`X-Frame-Options: DENY`، `X-Content-Type-Options: nosniff`،
`Referrer-Policy`، `Permissions-Policy`، `COOP`، و `HSTS` در production.

## ورودی خصمانه فرض می‌شود
- اعتبارسنجی در مرز با Pydantic، `extra="forbid"`.
- حذف کاراکترهای کنترلی و bidi، نرمال‌سازی NFC، سقف طول هر فیلد.
- تمام SQL پارامتری؛ شناسه‌های جدول/ستون با regex سفیدلیست می‌شوند.
- سقف حجم بدنه ۱۶ کیلوبایت.
- تله‌ی ربات (honeypot) که داده را ذخیره نمی‌کند و به ربات سیگنال نمی‌دهد.
- محدودیت نرخ روی ورود، ثبت نوبت و هر مسیر عمومی.

## حریم خصوصی در لاگ
`api/logging_.py` کلیدهای حساس را `[redacted]` و شماره/نام را به اثرانگشت
غیرقابل‌بازگشت (`fp_…` با HMAC کلید سرور) تبدیل می‌کند. تست
`test_logs_never_contain_a_phone_or_a_secret` این را تضمین می‌کند.

## گزارش آسیب‌پذیری
به `security@asaphysio.example` — پاسخ تا ۷۲ ساعت.
