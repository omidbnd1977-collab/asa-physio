# لایه ۱۰ — کش و CDN

## قانون پاک‌کردن کش

> **هیچ‌وقت نیازی به purge نیست، چون هیچ آدرسی دوبار محتوای متفاوت نمی‌دهد.**

`build_static.py` نام هر دارایی را به `name.<sha256-10>.ext` تبدیل می‌کند.
بایت جدید ⇒ آدرس جدید. تنها چیزی که آدرس ثابت دارد فایل‌های HTML هستند و
آن‌ها با `max-age=0, must-revalidate` سرو می‌شوند، پس همیشه با ETag اعتبارسنجی می‌شوند.

| نوع | Cache-Control | دلیل |
|---|---|---|
| `style.<hash>.css`, `*.<hash>.jpg`, `Vazirmatn-*.<hash>.woff2` | `public, max-age=31536000, immutable` | آدرس حاوی هش محتواست |
| `*.html` | `public, max-age=0, must-revalidate` + ETag | نقطه‌ی ورود، باید تازه باشد |
| `/api/admin/*` | `private, max-age=10` + ETag | داده‌ی کارکنان، هرگز در CDN |
| `/api/bookings` (POST) | بدون کش | نوشتن |

## چه زمانی **باید** دستی purge کنید
فقط اگر یک فایل HTML اشتباه منتشر شده باشد:
```bash
# Cloudflare
curl -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/purge_cache" \
  -H "Authorization: Bearer $CF_TOKEN" -H "Content-Type: application/json" \
  --data '{"files":["https://asaphysio.example/index.html"]}'
```
هرگز `purge_everything` نزنید؛ دارایی‌های هش‌دار نیازی ندارند و فقط هزینه‌ی پهنای باند می‌سازد.

## کش سمت سرور
`api/cache.py` نتیجه‌ی خواندن‌های یکسان پنل را به مدت `API_CACHE_TTL_S` نگه می‌دارد و
هر نوشتن روی `bookings` با `cache.purge("admin:bookings")` آن را باطل می‌کند
(تست `test_write_purges_the_cached_read`).
