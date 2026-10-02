# لایه ۱۳ — تمرین واقعی بازیابی

> تمرین‌نشده = نداشته. این سند گزارش یک بازیابی‌ست که **واقعاً اجرا شد**، نه یک برنامه.

## اجرای ۱ — ۲۰۲۶/۱۰/۰۱ (محیط staging)

```
$ ops/backup.sh
snapshot ok, bookings=50
backup: data/backups/asa-20261001T070506Z.db.gz (20K)
kept: 1 backups
real    0m0.087s

$ ops/restore.sh data/backups/asa-20261001T070506Z.db.gz data/drill.db
restored: 11 tables, 50 bookings, 2 migrations applied
restore completed in 0s
RTO measured: 38 ms

bookings restored: 50
sample: {'public_id': '3dbf28e0c95e', 'name': 'بیمار تست 44', 'phone': '09120000044'}
integrity: ok
```

| سنجه | مقدار |
|---|---|
| زمان تهیه‌ی پشتیبان | ۸۷ میلی‌ثانیه |
| **RTO اندازه‌گیری‌شده** (زمان بازگشت به کار) | **۳۸ میلی‌ثانیه** |
| **RPO** (بیشترین داده‌ی قابل‌از‌دست‌رفتن) | **۶ ساعت** — بازه‌ی سرویس `backup` در `docker-compose.yml` |
| رکوردهای بازیابی‌شده | ۵۰ از ۵۰ |
| `PRAGMA integrity_check` | ok |
| `PRAGMA foreign_key_check` | بدون تخلف |

## آنچه تمرین نشان داد
- `s.backup(d)` روی پایگاه‌داده‌ی در حال سرویس‌دهی کار می‌کند؛ نیاز به توقف سرویس نیست.
- `restore.sh` قبل از بازنویسی، نسخه‌ی فعلی را کنار می‌گذارد
  (`asa.db.before-restore-<stamp>`)، پس یک بازیابی اشتباه هم برگشت‌پذیر است.
- فایل‌های `-wal` و `-shm` باید حذف شوند وگرنه SQLite داده‌ی قدیمی را دوباره بازی می‌کند.
  این در اسکریپت هست.

## خودکارسازی
سرویس `backup` در `docker-compose.yml` هر ۶ ساعت `ops/backup.sh` را اجرا می‌کند.
`BACKUP_KEEP=14` یعنی ۱۴ نسخه‌ی آخر (~۳٫۵ روز) نگه داشته می‌شود.

> **تمرین بعدی: ۱۴۰۴/۱۰/۰۱.** بازیابی باید روی یک ماشین تازه و فقط از روی
> `data/backups/` انجام شود، بدون دسترسی به پایگاه‌داده‌ی اصلی.
