# Backup & Restore — ASA Physio

Production-safe procedure for SQLite + uploads. **Never print secrets or patient
phone/name values in logs.**

## What a backup contains

Each successful run creates a **bundle directory**:

```text
data/backups/asa-YYYYMMDDTHHMMSSZ/
  OK                      # present only if the bundle is complete
  manifest.json           # stamp, counts, sha256 (no secrets / no PII phones)
  database.sqlite.gz
  database.sqlite.gz.sha256
  uploads.tar.gz          # may be empty
  uploads.tar.gz.sha256
  env.copy                # optional, mode 600, only if BACKUP_INCLUDE_ENV is set
```

Incomplete attempts are left as `data/backups/.failed-<stamp>/` **without** `OK`.
`verify` rejects any `.partial-*` / `.failed-*` path even if an `OK` file was planted there.

Permissions (Unix): backup parent and bundle dirs `0700`; DB/uploads/manifest/checksum/OK files `0600`.

Symlinks and non-regular files under uploads are rejected at backup time. On extract, only
regular files and directories are accepted; symlink/hardlink/device members and path
traversal (`../`) are refused on all supported Python versions.

## Create a backup

```bash
ops/backup.sh
# or:
python3 -m ops.backup_core backup
```

Environment (optional):

| Variable | Default | Purpose |
|----------|---------|---------|
| `DB_PATH` | `data/asa.db` | Live SQLite file |
| `UPLOAD_DIR` | `data/uploads` | Patient/staff uploads |
| `BACKUP_DIR` | `data/backups` | Bundle parent |
| `BACKUP_KEEP` | `14` | Retention of complete `asa-*` bundles |
| `BACKUP_INCLUDE_ENV` | unset | Optional path to copy (mode 600; never logged) |

Online snapshot uses the SQLite backup API (safe while the app is serving).

## Restore (experimental first)

```bash
# 1) restore into an isolated folder (default under data/restore-work/<stamp>)
ops/restore.sh data/backups/asa-YYYYMMDDTHHMMSSZ

# 2) or choose the destination
ops/restore.sh data/backups/asa-YYYYMMDDTHHMMSSZ /tmp/asa-drill

# 3) only after verification, promote onto live paths (parks previous DB/uploads)
ops/restore.sh data/backups/asa-YYYYMMDDTHHMMSSZ /tmp/asa-drill --promote
```

Promotion parks:

- `asa.db.before-promote-<stamp>`
- `uploads.before-promote-<stamp>/`

## Legacy DB-only archives

Older `asa-*.db.gz` files (DB only, no uploads) still restore with an **explicit**
destination. Overwriting the live `DB_PATH` requires `--confirm-live-overwrite`:

```bash
ops/restore.sh data/backups/asa-20261001T070506Z.db.gz data/drill.db
# live overwrite (explicit confirm required):
ops/restore.sh data/backups/asa-….db.gz "$DB_PATH" --confirm-live-overwrite
```

## Verify without restoring

```bash
python3 -m ops.backup_core verify data/backups/asa-YYYYMMDDTHHMMSSZ
```

## Production notes

1. Keep SMS/reminder settings unchanged when running backup.
2. Prefer a pre-deploy bundle before every release.
3. After restore/promote, restart the app process so it reopens SQLite handles.
4. Uploads live under `UPLOAD_DIR` (default `data/uploads`) and are included in v2 bundles.
5. Do not treat a directory as valid unless `OK` + matching SHA256 files are present.
