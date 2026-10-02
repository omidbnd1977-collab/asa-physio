# لایه ۱۴ — سنجش نتیجه

## یک عدد

> **تعداد جلسه‌ی فیزیوتراپی که واقعاً انجام شد.**
> کلید: `session_attended` · `api/metrics.py::NORTH_STAR`

کلینیک از نوبت رزروشده پول درنمی‌آورد؛ از جلسه‌ای که بیمار سر آن حاضر شده
درمی‌آورد. این عدد فقط وقتی بالا می‌رود که کارکنان نوبت را «انجام شد» علامت بزنند.

**مسیر (funnel) تا این عدد**، همه از روز اول اندازه‌گیری می‌شوند:

```
patient_registered  →  appointment_booked  →  session_attended
   (ثبت‌نام)              (رزرو نوبت)           (جلسه‌ی انجام‌شده)
                   book_rate              show_rate
```

`show_rate` مهم‌ترین عدد عملیاتی است: اگر پایین بیاید یعنی بیماران نمی‌آیند،
و پیامک تأیید دو ساعت قبل دقیقاً برای بالا بردن همین عدد ساخته شده است.

## چرا این و نه چیز دیگر

| عدد | چرا north star نیست |
|---|---|
| بازدید صفحه | هیچ‌چیز درباره‌ی بیمار نمی‌گوید |
| ارسال فرم (`booking_submitted`) | ممکن است ارسال شود و هرگز به کلینیک نرسد — دقیقاً همان ایرادی که ممیزی پیدا کرد |
| نوبت رزروشده (`appointment_booked`) | رزرو بدون حضور، درآمد و درمان نیست — به‌عنوان مرحله‌ی میانی نگه داشته شده |
| کلیک روی تماس | شاید تماس گرفته، شاید نه. قابل اثبات نیست |

`booking_submitted`، `booking_delivery_failed` و `delivery_success_rate` به‌عنوان
**پشتیبان** نگه داشته می‌شوند، و `call_click` / `instagram_click` صراحتاً زیر کلید
`vanity` قرار دارند تا کسی اشتباهشان نگیرد. در پنل هم با برچسب «(تزئینی)» نمایش داده می‌شوند.

## از روز اول اندازه‌گیری می‌شود
`outcome_events` در migration شماره‌ی **۰۰۰۱** ساخته شده، نه بعداً.
هر رویداد در لحظه نوشته می‌شود؛ هیچ عددی بعداً بازسازی یا تخمین زده نمی‌شود.

## دیدنش
- پنل `/admin` → کارت «سنجش نتیجه» → کادر برجسته‌ی اول.
- API: `GET /api/admin/metrics?days=30`

```json
{"north_star": {"key": "session_attended",
                "label_fa": "جلسه‌ی فیزیوتراپی که واقعاً انجام شد",
                "value": 2, "window_days": 30, "all_time": 2},
 "funnel": {"patient_registered": 10, "appointment_booked": 12,
            "session_attended": 2, "book_rate": 120.0, "show_rate": 16.7},
 "supporting": {"booking_submitted": 3, "booking_delivered": 3,
                "booking_delivery_failed": 0, "delivery_success_rate": 100.0,
                "attendance_confirmed": 2},
 "vanity": {"call_click": 0, "instagram_click": 0}}
```

## هدف پیشنهادی برای سه ماه اول
| سنجه | ماه اول | ماه سوم |
|---|---|---|
| `session_attended` | ≥ ۳۰ | ≥ ۹۰ |
| `show_rate` | ≥ ۷۰٪ | ≥ ۸۵٪ |
| `delivery_success_rate` | ≥ ۹۹٪ | ≥ ۹۹٪ |

اگر `delivery_success_rate` زیر ۹۹٪ آمد مشکل **فنی** است (کانال پیامک یا تلگرام).
اگر `show_rate` پایین بود مشکل **عملیاتی** است — یادآوری زودتر، یا تماس تلفنی.
