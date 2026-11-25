"""Local-only drills for ops.backup_core (DB + uploads). Never touches Production."""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from ops.backup_core import (
    BackupError,
    create_backup,
    promote_restore,
    restore_bundle,
    restore_legacy_db_gz,
    unpack_uploads,
    verify_bundle,
)


def _seed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version TEXT PRIMARY KEY);
            INSERT INTO schema_migrations(version) VALUES ('0017');
            CREATE TABLE patients(
              id INTEGER PRIMARY KEY,
              public_id TEXT,
              national_id TEXT,
              full_name TEXT,
              phone TEXT
            );
            CREATE TABLE appointments(
              id INTEGER PRIMARY KEY,
              public_id TEXT,
              patient_id INTEGER,
              status TEXT,
              slot_date TEXT,
              slot_time TEXT
            );
            CREATE TABLE bookings(
              id INTEGER PRIMARY KEY,
              public_id TEXT
            );
            INSERT INTO patients VALUES (1,'p1','001','بیمار آزمایشی','09120000000');
            INSERT INTO patients VALUES (2,'p2','002','بیمار دوم','09121111111');
            INSERT INTO appointments VALUES (1,'a1',1,'booked','2026-11-26','16:00');
            INSERT INTO appointments VALUES (2,'a2',2,'cancelled','2026-11-27','17:30');
            INSERT INTO bookings VALUES (1,'legacy1');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _seed_uploads(upload_dir: Path) -> list[str]:
    upload_dir.mkdir(parents=True, exist_ok=True)
    names = ["med-aaaa1111bbbb2222.jpg", "mri-zz99.png"]
    (upload_dir / names[0]).write_bytes(b"\xff\xd8\xff\xe0" + b"fakejpeg-test")
    (upload_dir / names[1]).write_bytes(b"\x89PNG\r\n\x1a\n" + b"fakepng-test")
    nested = upload_dir / "nested"
    nested.mkdir()
    (nested / "note.txt").write_text("synthetic only\n", encoding="utf-8")
    names.append("nested/note.txt")
    return names


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_backup_restore_round_trip_with_uploads(tmp_path: Path):
    db = tmp_path / "live" / "asa.db"
    uploads = tmp_path / "live" / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    names = _seed_uploads(uploads)

    result = create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)
    assert (result.bundle_dir / "OK").is_file()
    assert not (bak / f".partial-{result.stamp}" / "OK").exists()
    man = verify_bundle(result.bundle_dir)
    assert man["database"]["counts"]["appointments"] == 2
    assert man["uploads"]["file_count"] == 3
    assert "0912" not in json.dumps(man)

    dest = tmp_path / "restore-work" / result.stamp
    out = restore_bundle(result.bundle_dir, dest)
    assert out["counts"]["appointments"] == 2
    assert out["upload_files"] == 3
    restored_db = Path(out["db"])
    conn = sqlite3.connect(restored_db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 2
    finally:
        conn.close()
    for name in names:
        assert (Path(out["uploads"]) / name).is_file()


def test_incomplete_backup_is_not_ok(tmp_path: Path, monkeypatch):
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    uploads.mkdir()

    def boom(*_a, **_k):
        raise BackupError("forced upload failure")

    monkeypatch.setattr("ops.backup_core.pack_uploads", boom)
    with pytest.raises(BackupError, match="forced upload failure"):
        create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)

    assert list(bak.glob("asa-*")) == []
    failed = list(bak.glob(".failed-*"))
    assert failed
    assert not (failed[0] / "OK").exists()
    assert (failed[0] / "FAILED").is_file()
    with pytest.raises(BackupError, match="incomplete|non-final|refusing"):
        verify_bundle(failed[0])


def test_partial_bundle_with_ok_is_rejected(tmp_path: Path):
    partial = tmp_path / ".partial-fake"
    partial.mkdir()
    (partial / "OK").write_text("ok\n", encoding="utf-8")
    (partial / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(BackupError, match="incomplete|non-final|refusing"):
        verify_bundle(partial)


def test_backup_permissions(tmp_path: Path):
    if os.name == "nt":
        pytest.skip("Unix permission bits are not enforced on Windows")
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    _seed_uploads(uploads)
    result = create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)
    assert _mode(bak) == 0o700
    assert _mode(result.bundle_dir) == 0o700
    for name in (
        "database.sqlite.gz",
        "uploads.tar.gz",
        "manifest.json",
        "OK",
        "database.sqlite.gz.sha256",
        "uploads.tar.gz.sha256",
    ):
        assert _mode(result.bundle_dir / name) == 0o600


def test_symlink_outside_uploads_rejected(tmp_path: Path):
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    _seed_db(db)
    uploads.mkdir()
    link = uploads / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not available in this environment")
    with pytest.raises(BackupError, match="symlink"):
        create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)


def test_path_traversal_in_tar_rejected(tmp_path: Path):
    dest = tmp_path / "out"
    tar_path = tmp_path / "evil.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="../escape.txt")
        data = b"pwn"
        info.size = len(data)
        import io

        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(BackupError, match="unsafe"):
        unpack_uploads(tar_path, dest)
    assert not (tmp_path / "escape.txt").exists()


def test_symlink_tar_member_rejected(tmp_path: Path):
    dest = tmp_path / "out"
    tar_path = tmp_path / "sym.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        info = tarfile.TarInfo(name="link-name")
        info.type = tarfile.SYMTYPE
        info.linkname = "/tmp/x"
        tar.addfile(info)
    with pytest.raises(BackupError, match="disallowed"):
        unpack_uploads(tar_path, dest)


def test_restore_failure_preserves_previous_experimental(tmp_path: Path):
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    _seed_uploads(uploads)
    result = create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)

    dest = tmp_path / "restore-work" / "keep-me"
    restore_bundle(result.bundle_dir, dest)
    marker = dest / "asa.db"
    original = marker.read_bytes()
    (dest / "sentinel.txt").write_text("keep\n", encoding="utf-8")

    bad = bak / "asa-corrupt"
    bad.mkdir()
    (bad / "OK").write_text("ok\n", encoding="utf-8")
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "format": 2,
                "status": "ok",
                "database": {"file": "database.sqlite.gz", "sha256": "0" * 64, "counts": {}},
                "uploads": {"file": "uploads.tar.gz", "sha256": "1" * 64, "file_count": 0},
            }
        ),
        encoding="utf-8",
    )
    (bad / "database.sqlite.gz").write_bytes(b"not-a-gzip")
    (bad / "uploads.tar.gz").write_bytes(b"not-a-tar")

    with pytest.raises(BackupError):
        restore_bundle(bad, dest)
    assert marker.read_bytes() == original
    assert (dest / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"


def test_promote_parks_previous_live_paths(tmp_path: Path):
    live_db = tmp_path / "prod" / "asa.db"
    live_up = tmp_path / "prod" / "uploads"
    bak = tmp_path / "backups"
    _seed_db(live_db)
    _seed_uploads(live_up)
    (live_up / "old-only.bin").write_bytes(b"old")

    seed_db = tmp_path / "seed" / "asa.db"
    seed_up = tmp_path / "seed" / "uploads"
    _seed_db(seed_db)
    _seed_uploads(seed_up)
    result = create_backup(db_path=seed_db, upload_dir=seed_up, backup_dir=bak, keep=5)
    exp = tmp_path / "exp"
    restore_bundle(result.bundle_dir, exp)
    promo = promote_restore(exp, live_db=live_db, live_uploads=live_up, bundle=result.bundle_dir)
    assert Path(promo["live_db"]).is_file()
    assert (live_up / "med-aaaa1111bbbb2222.jpg").is_file()
    assert promo["parked"].get("db")
    assert Path(promo["parked"]["db"]).is_file()


def test_promote_rejects_tampered_experimental_before_swap(tmp_path: Path):
    bak = tmp_path / "backups"
    seed_db = tmp_path / "seed" / "asa.db"
    seed_up = tmp_path / "seed" / "uploads"
    live_db = tmp_path / "prod" / "asa.db"
    live_up = tmp_path / "prod" / "uploads"
    _seed_db(seed_db)
    _seed_uploads(seed_up)
    _seed_db(live_db)
    _seed_uploads(live_up)
    original_live = live_db.read_bytes()
    result = create_backup(db_path=seed_db, upload_dir=seed_up, backup_dir=bak, keep=5)
    exp = tmp_path / "exp"
    restore_bundle(result.bundle_dir, exp)
    # Tamper after restore, before promote.
    (exp / "uploads" / "extra-evil.bin").write_bytes(b"evil")
    with pytest.raises(BackupError, match="upload count mismatch|no longer matches"):
        promote_restore(exp, live_db=live_db, live_uploads=live_up, bundle=result.bundle_dir)
    assert live_db.read_bytes() == original_live


def test_promote_rejects_tampered_bundle_after_restore(tmp_path: Path):
    bak = tmp_path / "backups"
    seed_db = tmp_path / "seed" / "asa.db"
    seed_up = tmp_path / "seed" / "uploads"
    live_db = tmp_path / "prod" / "asa.db"
    live_up = tmp_path / "prod" / "uploads"
    _seed_db(seed_db)
    _seed_uploads(seed_up)
    _seed_db(live_db)
    _seed_uploads(live_up)
    original_live = live_db.read_bytes()
    result = create_backup(db_path=seed_db, upload_dir=seed_up, backup_dir=bak, keep=5)
    exp = tmp_path / "exp"
    restore_bundle(result.bundle_dir, exp)
    # Tamper the on-disk bundle after a successful experimental restore.
    gz = result.bundle_dir / "database.sqlite.gz"
    gz.write_bytes(gz.read_bytes() + b"\x00")
    with pytest.raises(BackupError, match="sha256|integrity|mismatch"):
        promote_restore(exp, live_db=live_db, live_uploads=live_up, bundle=result.bundle_dir)
    assert live_db.read_bytes() == original_live


def test_promote_db_swap_failure_preserves_live(tmp_path: Path, monkeypatch):
    bak = tmp_path / "backups"
    seed_db = tmp_path / "seed" / "asa.db"
    seed_up = tmp_path / "seed" / "uploads"
    live_db = tmp_path / "prod" / "asa.db"
    live_up = tmp_path / "prod" / "uploads"
    _seed_db(seed_db)
    _seed_uploads(seed_up)
    _seed_db(live_db)
    _seed_uploads(live_up)
    (live_up / "keep-me.bin").write_bytes(b"keep")
    original_live = live_db.read_bytes()
    result = create_backup(db_path=seed_db, upload_dir=seed_up, backup_dir=bak, keep=5)
    exp = tmp_path / "exp"
    restore_bundle(result.bundle_dir, exp)

    def boom(*_a, **_k):
        raise BackupError("forced db swap failure")

    monkeypatch.setattr("ops.backup_core._swap_file_keep_previous", boom)
    with pytest.raises(BackupError, match="forced db swap failure"):
        promote_restore(exp, live_db=live_db, live_uploads=live_up, bundle=result.bundle_dir)
    assert live_db.read_bytes() == original_live
    assert (live_up / "keep-me.bin").is_file()


def test_promote_upload_failure_preserves_previous(tmp_path: Path, monkeypatch):
    bak = tmp_path / "backups"
    seed_db = tmp_path / "seed" / "asa.db"
    seed_up = tmp_path / "seed" / "uploads"
    live_db = tmp_path / "prod" / "asa.db"
    live_up = tmp_path / "prod" / "uploads"
    _seed_db(seed_db)
    _seed_uploads(seed_up)
    _seed_db(live_db)
    _seed_uploads(live_up)
    (live_up / "keep-me.bin").write_bytes(b"keep")
    original_live_db = live_db.read_bytes()
    result = create_backup(db_path=seed_db, upload_dir=seed_up, backup_dir=bak, keep=5)
    exp = tmp_path / "exp"
    restore_bundle(result.bundle_dir, exp)

    def flaky(new_dir, live_dir, park_dir):
        # Park previous uploads then fail before installing incoming.
        if live_dir.exists():
            live_dir.rename(park_dir)
        raise BackupError("forced uploads swap failure")

    monkeypatch.setattr("ops.backup_core._swap_dir_keep_previous", flaky)
    with pytest.raises(BackupError, match="forced uploads swap failure"):
        promote_restore(exp, live_db=live_db, live_uploads=live_up, bundle=result.bundle_dir)
    # Failed promote must restore prior live DB + uploads (not leave a half-applied state).
    assert live_db.read_bytes() == original_live_db
    assert live_up.is_dir()
    assert (live_up / "keep-me.bin").is_file()


def test_legacy_db_gz_restore_requires_confirm_for_live(tmp_path: Path, monkeypatch):
    import gzip

    db = tmp_path / "asa.db"
    _seed_db(db)
    gz = tmp_path / "asa-legacy.db.gz"
    gz.write_bytes(gzip.compress(db.read_bytes()))
    live = tmp_path / "live" / "asa.db"
    live.parent.mkdir(parents=True)
    live.write_bytes(db.read_bytes())
    monkeypatch.setenv("DB_PATH", str(live))
    # Re-import defaults pick env at call time via _default_paths.
    with pytest.raises(BackupError, match="confirm-live-overwrite"):
        restore_legacy_db_gz(gz, live, live_db=live, confirm_live_overwrite=False)
    out = restore_legacy_db_gz(gz, live, live_db=live, confirm_live_overwrite=True)
    assert out["legacy"] is True
    dest = tmp_path / "out" / "asa.db"
    out2 = restore_legacy_db_gz(gz, dest, live_db=live, confirm_live_overwrite=False)
    assert Path(out2["db"]).is_file()


def test_verify_rejects_missing_ok(tmp_path: Path):
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    uploads.mkdir()
    result = create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)
    (result.bundle_dir / "OK").unlink()
    with pytest.raises(BackupError, match="OK marker"):
        verify_bundle(result.bundle_dir)


def test_ok_not_present_in_partial_during_create(tmp_path: Path, monkeypatch):
    db = tmp_path / "asa.db"
    uploads = tmp_path / "uploads"
    bak = tmp_path / "backups"
    _seed_db(db)
    uploads.mkdir()

    seen_ok_in_partial = {"value": False}
    real_rename = Path.rename

    def wrapped_rename(self, target):
        if self.name.startswith(".partial-") and (self / "OK").exists():
            seen_ok_in_partial["value"] = True
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", wrapped_rename)
    create_backup(db_path=db, upload_dir=uploads, backup_dir=bak, keep=5)
    assert seen_ok_in_partial["value"] is False
