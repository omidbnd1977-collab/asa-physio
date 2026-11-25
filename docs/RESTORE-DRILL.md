# لایه ۱۳ — تمرین واقعی بازیابی

> تمرین‌نشده = نداشته. این سند گزارش بازیابی‌هایی است که **واقعاً اجرا شدند**.

فرمت فعلی باندل (v2): `docs/BACKUP-RESTORE.md` — شامل SQLite + `uploads/` + SHA256 + نشان `OK`.

## اجرای ۱ — ۲۰۲۶/۱۰/۰۱ (محیط staging، فرمت قدیمی DB-only)

```
$ ops/backup.sh
snapshot ok, bookings=50
backup: data/backups/asa-20261001T070506Z.db.gz (20K)
```

بازیابی قدیمی همچنان با `ops/restore.sh <asa-*.db.gz> <target.db>` پشتیبانی می‌شود.

## اجرای ۲ — ۲۰۲۶/۱۱/۲۵ (محیط محلی، فرمت v2)

تست خودکار: `tests/test_backup_restore.py` (داده ساختگی؛ بدون Production).

پوشش:

- snapshot آنلاین SQLite + `integrity_check` / `foreign_key_check`
- شمارش `appointments` / `patients` (نه فقط `bookings` قدیمی)
- آرشیو `uploads/` با حفظ مسیر نسبی
- نشان `OK` فقط پس از rename به مسیر نهایی؛ `.partial-*` / `.failed-*` حتی با OK رد می‌شوند
- مجوز Unix: پوشه `0700`، فایل‌های باندل `0600`
- رد symlink / path traversal / اعضای غیرعادی tar
- restore آزمایشی؛ شکست restore محتوای قبلی dest را نگه می‌دارد
- `--promote` با re-verify باندل؛ شکست uploads نسخهٔ قبلی را برمی‌گرداند
- legacy live overwrite فقط با `--confirm-live-overwrite`

## خودکارسازی
سرویس `backup` در `docker-compose.yml` هر ۶ ساعت `ops/backup.sh` را اجرا می‌کند.
`BACKUP_KEEP=14` یعنی ۱۴ باندل کامل آخر نگه داشته می‌شود.

> **تمرین بعدی روی VPS:** فقط پس از تأیید جداگانه؛ restore اول در
> `data/restore-work/<stamp>/`، سپس در صورت نیاز `--promote` و restart سرویس.
