"""Verified backup / restore for ASA Physio (DB + uploads).

Designed for local drills and production ops scripts. Never logs secrets or
patient phone/name payloads — only aggregate counts and file hashes.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FORMAT_VERSION = 2
BUNDLE_PREFIX = "asa-"
OK_MARKER = "OK"
MANIFEST_NAME = "manifest.json"
DB_GZ_NAME = "database.sqlite.gz"
UPLOADS_TAR_NAME = "uploads.tar.gz"
RESTORE_OK_NAME = "RESTORE_OK"

COUNT_TABLES = (
    "schema_migrations",
    "patients",
    "appointments",
    "patient_sessions",
    "sms_messages",
    "sms_inbound",
    "messages",
    "admin_users",
    "bookings",
    "feedback",
    "treatment_sessions",
)


class BackupError(RuntimeError):
    """Backup or restore failed; partial artifacts must not be treated as valid."""


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Best-effort on platforms that ignore Unix modes.
        pass


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def _secure_file(path: Path) -> None:
    _chmod(path, 0o600)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_sha256(path: Path) -> str:
    digest = _sha256_file(path)
    side = path.with_suffix(path.suffix + ".sha256")
    side.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    _secure_file(side)
    return digest


def _git_sha(repo: Path) -> str | None:
    try:
        git = shutil.which("git")
        if not git:
            return None
        out = subprocess.check_output(  # noqa: S603 — fixed argv: resolved git + rev-parse HEAD
            [git, "rev-parse", "HEAD"],
            cwd=repo,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return out.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _db_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    ]
    counts: dict[str, int] = {}
    for name in COUNT_TABLES:
        if _table_exists(conn, name):
            # name is a fixed allowlist identifier, never user input.
            counts[name] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]  # noqa: S608
            )
    latest = None
    if _table_exists(conn, "schema_migrations"):
        row = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        latest = row[0] if row else None
    return {
        "tables": tables,
        "table_count": len(tables),
        "counts": counts,
        "migrations_latest": latest,
    }


def snapshot_sqlite(src_db: Path, dst_db: Path) -> dict[str, Any]:
    """Online-consistent copy via sqlite3 backup API + integrity checks."""
    if not src_db.is_file():
        raise BackupError(f"database not found: {src_db}")
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    if dst_db.exists():
        dst_db.unlink()
    src = sqlite3.connect(str(src_db))
    try:
        dst = sqlite3.connect(str(dst_db))
        try:
            with dst:
                src.backup(dst)
            row = dst.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise BackupError(f"integrity_check failed: {row}")
            fk = dst.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise BackupError(f"foreign_key_check failed ({len(fk)} violations)")
            return _db_stats(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _gzip_file(src: Path, dst_gz: Path) -> None:
    with src.open("rb") as fin, gzip.open(dst_gz, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    _secure_file(dst_gz)


def _gunzip_file(src_gz: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(src_gz, "rb") as fin, dst.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1024 * 1024)
    _secure_file(dst)


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise BackupError(f"symlink not allowed in uploads: {path}")
    try:
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            raise BackupError(f"symlink not allowed in uploads: {path}")
    except FileNotFoundError:
        return


def pack_uploads(upload_dir: Path, tar_gz: Path) -> dict[str, Any]:
    """Tar.gz upload tree preserving relative paths. Empty archive if dir missing/empty."""
    tar_gz.parent.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    if not upload_dir.exists():
        with tarfile.open(tar_gz, "w:gz") as tar:
            pass
        _secure_file(tar_gz)
        return {"file_count": 0, "bytes": 0}

    root = upload_dir.resolve()
    if not root.is_dir():
        raise BackupError(f"uploads path is not a directory: {upload_dir}")
    _reject_symlink(upload_dir)

    for p in sorted(root.rglob("*")):
        _reject_symlink(p)
        if p.is_dir():
            continue
        if not p.is_file():
            raise BackupError(f"non-regular upload entry not allowed: {p}")
        try:
            p.resolve().relative_to(root)
        except ValueError as exc:
            raise BackupError(f"upload path escapes root: {p}") from exc
        files.append(p)

    with tarfile.open(tar_gz, "w:gz") as tar:
        for p in files:
            arcname = p.relative_to(root).as_posix()
            # Add as a regular file without following a symlink (already rejected).
            tar.add(p, arcname=arcname, recursive=False)
    _secure_file(tar_gz)
    return {
        "file_count": len(files),
        "bytes": int(sum(p.stat().st_size for p in files)),
    }


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("../") or normalized == "..":
        raise BackupError(f"unsafe upload member: {name}")
    if normalized.startswith("..") or "/../" in f"/{normalized}/":
        raise BackupError(f"unsafe upload member: {name}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise BackupError(f"unsafe upload member: {name}")
    if any(p.startswith(":") or (len(p) == 2 and p[1] == ":") for p in parts):
        raise BackupError(f"unsafe upload member: {name}")
    return "/".join(parts)


def _is_within(candidate: Path, root: Path) -> bool:
    """True iff candidate resolves strictly under root (path-traversal safe)."""
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def unpack_uploads(tar_gz: Path, dest_dir: Path) -> int:
    """Extract only regular files under dest_dir; reject links/devices/traversal."""
    _secure_dir(dest_dir)
    dest_root = dest_dir.resolve()
    count = 0
    with tarfile.open(tar_gz, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isdir():
                rel = _safe_member_name(member.name)
                target = (dest_root / rel).resolve()
                if target != dest_root and not _is_within(target, dest_root):
                    raise BackupError(f"unsafe upload member: {member.name}")
                _secure_dir(target)
                continue
            # Only plain regular files.
            if (
                member.issym()
                or member.islnk()
                or member.isfifo()
                or member.ischr()
                or member.isblk()
            ):
                raise BackupError(f"disallowed tar member type: {member.name}")
            if not member.isreg():
                raise BackupError(f"disallowed tar member type: {member.name}")
            rel = _safe_member_name(member.name)
            target = (dest_root / rel).resolve()
            if not _is_within(target, dest_root):
                raise BackupError(f"unsafe upload member: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            _chmod(target.parent, 0o700)
            src = tar.extractfile(member)
            if src is None:
                raise BackupError(f"cannot read tar member: {member.name}")
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            _secure_file(target)
            count += 1
    return count


@dataclass
class BackupResult:
    bundle_dir: Path
    stamp: str
    manifest: dict[str, Any]


def create_backup(
    *,
    db_path: Path,
    upload_dir: Path,
    backup_dir: Path,
    repo_root: Path | None = None,
    keep: int = 14,
    include_env: Path | None = None,
) -> BackupResult:
    """Create a complete verified bundle. Partial dirs are never marked OK."""
    stamp = _utc_stamp()
    _secure_dir(backup_dir)
    partial = backup_dir / f".partial-{stamp}"
    final = backup_dir / f"{BUNDLE_PREFIX}{stamp}"
    if partial.exists():
        shutil.rmtree(partial)
    _secure_dir(partial)

    try:
        raw_db = partial / "database.sqlite"
        stats = snapshot_sqlite(db_path, raw_db)
        _secure_file(raw_db)
        db_gz = partial / DB_GZ_NAME
        _gzip_file(raw_db, db_gz)
        verify_db = partial / "database.verify.sqlite"
        _gunzip_file(db_gz, verify_db)
        vconn = sqlite3.connect(str(verify_db))
        try:
            if vconn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("gzip round-trip integrity_check failed")
        finally:
            vconn.close()
        verify_db.unlink(missing_ok=True)
        raw_db.unlink(missing_ok=True)
        db_sha = _write_sha256(db_gz)

        uploads_tar = partial / UPLOADS_TAR_NAME
        upload_stats = pack_uploads(upload_dir, uploads_tar)
        uploads_sha = _write_sha256(uploads_tar)

        env_meta = None
        if include_env is not None:
            if not include_env.is_file() or include_env.is_symlink():
                raise BackupError(f"env file not found or is a symlink: {include_env}")
            dest_env = partial / "env.copy"
            shutil.copy2(include_env, dest_env)
            _secure_file(dest_env)
            env_meta = {
                "file": "env.copy",
                "sha256": _write_sha256(dest_env),
                "mode": "600",
            }

        manifest = {
            "format": FORMAT_VERSION,
            "stamp": stamp,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_sha": _git_sha(repo_root) if repo_root else None,
            "status": "ok",
            "database": {
                "file": DB_GZ_NAME,
                "sha256": db_sha,
                "integrity": "ok",
                **stats,
            },
            "uploads": {
                "file": UPLOADS_TAR_NAME,
                "sha256": uploads_sha,
                **upload_stats,
            },
            "env": env_meta,
        }
        man_path = partial / MANIFEST_NAME
        man_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _secure_file(man_path)

        # Never write OK into .partial — only after the bundle is in its final path.
        if (partial / OK_MARKER).exists():
            (partial / OK_MARKER).unlink()
        if final.exists():
            raise BackupError(f"bundle already exists: {final}")
        partial.rename(final)
        _chmod(final, 0o700)
        for child in final.iterdir():
            if child.is_file():
                _secure_file(child)
        (final / OK_MARKER).write_text("ok\n", encoding="utf-8")
        _secure_file(final / OK_MARKER)
    except Exception:
        try:
            failed = backup_dir / f".failed-{stamp}"
            if partial.exists():
                if failed.exists():
                    shutil.rmtree(failed)
                partial.rename(failed)
            if failed.exists():
                (failed / OK_MARKER).unlink(missing_ok=True)
                (failed / "FAILED").write_text("incomplete\n", encoding="utf-8")
                _chmod(failed, 0o700)
        except OSError:
            shutil.rmtree(partial, ignore_errors=True)
        raise

    _prune_old_bundles(backup_dir, keep=keep)
    return BackupResult(bundle_dir=final, stamp=stamp, manifest=manifest)


def _prune_old_bundles(backup_dir: Path, *, keep: int) -> None:
    if keep < 1:
        return
    bundles = sorted(
        [p for p in backup_dir.iterdir() if p.is_dir() and p.name.startswith(BUNDLE_PREFIX)],
        key=lambda p: p.name,
        reverse=True,
    )
    for old in bundles[keep:]:
        if (old / OK_MARKER).is_file():
            shutil.rmtree(old, ignore_errors=True)


def _assert_final_bundle_path(bundle: Path) -> None:
    name = bundle.name
    if name.startswith(".partial-") or name.startswith(".failed-") or name.startswith("."):
        raise BackupError(f"refusing incomplete/non-final bundle path: {bundle}")
    if not name.startswith(BUNDLE_PREFIX):
        raise BackupError(f"refusing unexpected bundle name: {bundle}")


def verify_bundle(bundle: Path) -> dict[str, Any]:
    """Validate a complete backup bundle. Raises BackupError if invalid."""
    bundle = bundle.resolve()
    if not bundle.is_dir():
        raise BackupError(f"not a bundle directory: {bundle}")
    _assert_final_bundle_path(bundle)
    if not (bundle / OK_MARKER).is_file():
        raise BackupError("missing OK marker — incomplete or failed backup")
    man_path = bundle / MANIFEST_NAME
    if not man_path.is_file():
        raise BackupError("missing manifest.json")
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ok" or int(manifest.get("format", 0)) < 2:
        raise BackupError("manifest status/format not acceptable")

    db_gz = bundle / DB_GZ_NAME
    if not db_gz.is_file() or db_gz.is_symlink():
        raise BackupError("missing database.sqlite.gz")
    db_sha = _sha256_file(db_gz)
    if db_sha != manifest["database"]["sha256"]:
        raise BackupError("database sha256 mismatch")
    side = db_gz.with_suffix(db_gz.suffix + ".sha256")
    if side.is_file() and _sha256_file(db_gz) != side.read_text(encoding="utf-8").split()[0]:
        raise BackupError("database sidecar sha256 mismatch")

    uploads_tar = bundle / UPLOADS_TAR_NAME
    if not uploads_tar.is_file() or uploads_tar.is_symlink():
        raise BackupError("missing uploads.tar.gz")
    up_sha = _sha256_file(uploads_tar)
    if up_sha != manifest["uploads"]["sha256"]:
        raise BackupError("uploads sha256 mismatch")
    side_u = uploads_tar.with_suffix(uploads_tar.suffix + ".sha256")
    if side_u.is_file() and up_sha != side_u.read_text(encoding="utf-8").split()[0]:
        raise BackupError("uploads sidecar sha256 mismatch")

    with tempfile.TemporaryDirectory(prefix="asa-bak-verify-") as tmp:
        tmp_db = Path(tmp) / "check.sqlite"
        _gunzip_file(db_gz, tmp_db)
        conn = sqlite3.connect(str(tmp_db))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("bundle DB integrity_check failed")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise BackupError(f"bundle foreign_key_check failed ({len(fk)})")
            if not _table_exists(conn, "appointments"):
                raise BackupError("appointments table missing in backup")
            if not _table_exists(conn, "patients"):
                raise BackupError("patients table missing in backup")
        finally:
            conn.close()
    return manifest


def _swap_file_keep_previous(new_file: Path, live_file: Path, park_path: Path) -> None:
    """Install new_file over live_file; park a copy of previous live first.

    Previous live remains intact until os.replace succeeds.
    """
    if live_file.exists():
        shutil.copy2(live_file, park_path)
        _secure_file(park_path)
    for suffix in ("-wal", "-shm"):
        Path(str(live_file) + suffix).unlink(missing_ok=True)
    os.replace(new_file, live_file)
    _secure_file(live_file)


def _swap_dir_keep_previous(new_dir: Path, live_dir: Path, park_dir: Path) -> None:
    """Replace live_dir with new_dir; park previous live_dir by rename when present."""
    if live_dir.exists():
        if park_dir.exists():
            shutil.rmtree(park_dir)
        live_dir.rename(park_dir)
    try:
        new_dir.rename(live_dir)
        _chmod(live_dir, 0o700)
    except Exception:
        # Roll live back from park if the incoming rename failed.
        if park_dir.exists() and not live_dir.exists():
            park_dir.rename(live_dir)
        raise


def _unlink_sqlite_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)


def _rollback_file_swap(live_file: Path, park_path: Path, *, existed_before: bool) -> None:
    """Restore pre-swap file state (previous content, or absence)."""
    if park_path.exists():
        os.replace(park_path, live_file)
        _secure_file(live_file)
        return
    if not existed_before:
        live_file.unlink(missing_ok=True)
        _unlink_sqlite_sidecars(live_file)


def _rollback_dir_swap(live_dir: Path, park_dir: Path, *, existed_before: bool) -> None:
    """Restore pre-swap directory state (previous tree, or absence)."""
    if park_dir.exists():
        if live_dir.exists():
            shutil.rmtree(live_dir, ignore_errors=True)
        park_dir.rename(live_dir)
        _chmod(live_dir, 0o700)
        return
    if not existed_before and live_dir.exists():
        shutil.rmtree(live_dir, ignore_errors=True)


def _upload_file_map(root: Path) -> dict[str, str]:
    """Map relative posix paths -> sha256 for regular files under root."""
    if not root.is_dir():
        raise BackupError(f"uploads directory missing: {root}")
    root_res = root.resolve()
    mapping: dict[str, str] = {}
    for p in sorted(root_res.rglob("*")):
        if p.is_dir():
            continue
        if p.is_symlink() or not p.is_file():
            raise BackupError(f"non-regular upload entry not allowed: {p.name}")
        rel = p.relative_to(root_res).as_posix()
        mapping[rel] = _sha256_file(p)
    return mapping


def _assert_upload_maps_equal(expected: dict[str, str], actual: dict[str, str]) -> None:
    """Fail if relative paths or per-file hashes diverge (no file contents logged)."""
    if expected == actual:
        return
    missing = len(set(expected) - set(actual))
    extra = len(set(actual) - set(expected))
    changed = sum(1 for k, digest in expected.items() if k in actual and actual[k] != digest)
    raise BackupError(
        "experimental uploads mismatch: "
        f"missing={missing}, extra={extra}, content_changed={changed}"
    )


def restore_bundle(
    bundle: Path,
    dest_dir: Path,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    """Restore DB + uploads into an experimental directory (never touches live paths)."""
    dest_dir = dest_dir.resolve()
    _secure_dir(dest_dir)
    stamp = _utc_stamp()
    staging = dest_dir / f".restore-staging-{stamp}"
    if staging.exists():
        shutil.rmtree(staging)
    _secure_dir(staging)

    try:
        manifest = (
            verify_bundle(bundle)
            if verify
            else json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))
        )
        if not verify:
            _assert_final_bundle_path(bundle.resolve())

        db_out = staging / "asa.db"
        _gunzip_file(bundle / DB_GZ_NAME, db_out)
        conn = sqlite3.connect(str(db_out))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("restored DB integrity_check failed")
            stats = _db_stats(conn)
        finally:
            conn.close()

        uploads_out = staging / "uploads"
        n_files = unpack_uploads(bundle / UPLOADS_TAR_NAME, uploads_out)
        expected = manifest.get("uploads", {}).get("file_count")
        if expected is not None and int(expected) != n_files:
            raise BackupError(f"upload file_count mismatch: expected {expected}, got {n_files}")

        final_db = dest_dir / "asa.db"
        final_uploads = dest_dir / "uploads"
        had_db = final_db.exists()
        had_uploads = final_uploads.exists()
        incoming_db = dest_dir / f".asa.db.incoming-{stamp}"
        incoming_up = dest_dir / f".uploads.incoming-{stamp}"
        if incoming_db.exists():
            incoming_db.unlink()
        if incoming_up.exists():
            shutil.rmtree(incoming_up)
        os.replace(db_out, incoming_db)
        uploads_out.rename(incoming_up)

        park_db = dest_dir / f"asa.db.before-restore-{stamp}"
        park_up = dest_dir / f"uploads.before-restore-{stamp}"
        db_swapped = False
        try:
            _swap_file_keep_previous(incoming_db, final_db, park_db)
            db_swapped = True
            _swap_dir_keep_previous(incoming_up, final_uploads, park_up)
        except Exception:
            incoming_db.unlink(missing_ok=True)
            if incoming_up.exists():
                shutil.rmtree(incoming_up, ignore_errors=True)
            # Full rollback: never leave dest half-applied.
            if db_swapped:
                try:
                    _rollback_file_swap(final_db, park_db, existed_before=had_db)
                except OSError:
                    pass
            try:
                _rollback_dir_swap(final_uploads, park_up, existed_before=had_uploads)
            except OSError:
                pass
            raise

        ok_path = dest_dir / RESTORE_OK_NAME
        ok_path.write_text(
            json.dumps(
                {
                    "restored_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "from_bundle": str(bundle.resolve()),
                    "stamp": manifest.get("stamp"),
                    "counts": stats.get("counts"),
                    "upload_files": n_files,
                    "db_sha256": manifest["database"]["sha256"],
                    "uploads_sha256": manifest["uploads"]["sha256"],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        _secure_file(ok_path)
        return {
            "dest_dir": str(dest_dir),
            "db": str(final_db),
            "uploads": str(final_uploads),
            "upload_files": n_files,
            "counts": stats.get("counts"),
            "manifest": manifest,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def promote_restore(
    experimental_dir: Path,
    *,
    live_db: Path,
    live_uploads: Path,
    bundle: Path | None = None,
) -> dict[str, Any]:
    """Promote a verified experimental restore onto live paths after re-validation."""
    experimental_dir = experimental_dir.resolve()
    src_db = experimental_dir / "asa.db"
    src_up = experimental_dir / "uploads"
    ok_path = experimental_dir / RESTORE_OK_NAME
    if not src_db.is_file() or not ok_path.is_file():
        raise BackupError("experimental restore incomplete (need asa.db + RESTORE_OK)")

    meta = json.loads(ok_path.read_text(encoding="utf-8"))
    bundle_path = Path(bundle) if bundle is not None else Path(meta["from_bundle"])
    manifest = verify_bundle(bundle_path)
    # Ensure experimental still matches the validated bundle hashes/counts.
    if meta.get("db_sha256") and meta["db_sha256"] != manifest["database"]["sha256"]:
        raise BackupError("experimental restore no longer matches bundle database hash")
    if meta.get("uploads_sha256") and meta["uploads_sha256"] != manifest["uploads"]["sha256"]:
        raise BackupError("experimental restore no longer matches bundle uploads hash")

    # Re-materialize bundle and compare DB bytes + per-file upload path/hash maps
    # before any live path is touched.
    with tempfile.TemporaryDirectory(prefix="asa-promote-check-") as tmp:
        expected_db = Path(tmp) / "expected.sqlite"
        _gunzip_file(bundle_path / DB_GZ_NAME, expected_db)
        if _sha256_file(expected_db) != _sha256_file(src_db):
            raise BackupError("experimental restore no longer matches bundle database bytes")
        expected_up = Path(tmp) / "expected-uploads"
        unpack_uploads(bundle_path / UPLOADS_TAR_NAME, expected_up)
        if not src_up.is_dir():
            raise BackupError("experimental uploads directory missing")
        _assert_upload_maps_equal(_upload_file_map(expected_up), _upload_file_map(src_up))

    conn = sqlite3.connect(str(src_db))
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("experimental DB failed integrity_check")
        stats = _db_stats(conn)
    finally:
        conn.close()

    stamp = _utc_stamp()
    parked: dict[str, str] = {}
    _secure_dir(live_db.parent)
    _secure_dir(live_uploads.parent)
    had_db = live_db.exists()
    had_uploads = live_uploads.exists()

    incoming_db = live_db.with_name(f".promote-db-incoming-{stamp}")
    incoming_up = live_uploads.with_name(f".promote-uploads-incoming-{stamp}")
    if incoming_db.exists():
        incoming_db.unlink()
    if incoming_up.exists():
        shutil.rmtree(incoming_up)
    shutil.copy2(src_db, incoming_db)
    _secure_file(incoming_db)
    shutil.copytree(src_up, incoming_up)
    _chmod(incoming_up, 0o700)

    park_db = live_db.with_name(f"{live_db.name}.before-promote-{stamp}")
    park_up = live_uploads.with_name(f"{live_uploads.name}.before-promote-{stamp}")
    db_swapped = False
    try:
        _swap_file_keep_previous(incoming_db, live_db, park_db)
        db_swapped = True
        if park_db.exists():
            parked["db"] = str(park_db)
        _swap_dir_keep_previous(incoming_up, live_uploads, park_up)
        if park_up.exists():
            parked["uploads"] = str(park_up)
    except Exception:
        incoming_db.unlink(missing_ok=True)
        if incoming_up.exists():
            shutil.rmtree(incoming_up, ignore_errors=True)
        if db_swapped:
            try:
                _rollback_file_swap(live_db, park_db, existed_before=had_db)
                parked.pop("db", None)
            except OSError:
                pass
        try:
            _rollback_dir_swap(live_uploads, park_up, existed_before=had_uploads)
            parked.pop("uploads", None)
        except OSError:
            pass
        raise

    return {
        "live_db": str(live_db),
        "live_uploads": str(live_uploads),
        "parked": parked,
        "counts": stats.get("counts"),
    }


def restore_legacy_db_gz(
    src_gz: Path,
    dest_db: Path,
    *,
    confirm_live_overwrite: bool = False,
    live_db: Path | None = None,
) -> dict[str, Any]:
    """Compatibility path for old asa-*.db.gz backups (DB only, no uploads)."""
    if not src_gz.is_file():
        raise BackupError(f"no such backup: {src_gz}")
    _root, default_live, _uploads, _backups = _default_paths()
    live = (live_db or default_live).resolve()
    dest = dest_db.resolve()
    if dest == live and not confirm_live_overwrite:
        raise BackupError("refusing to overwrite live database without --confirm-live-overwrite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    tmp = dest.with_name(f".legacy-restore-{stamp}.db")
    try:
        _gunzip_file(src_gz, tmp)
        conn = sqlite3.connect(str(tmp))
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("legacy restore integrity_check failed")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk:
                raise BackupError(f"legacy foreign_key_check failed ({len(fk)})")
            stats = _db_stats(conn)
        finally:
            conn.close()
        park = dest.with_name(f"{dest.name}.before-restore-{stamp}")
        _swap_file_keep_previous(tmp, dest, park)
        return {"db": str(dest), "counts": stats.get("counts"), "legacy": True}
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _default_paths() -> tuple[Path, Path, Path, Path]:
    root = Path(__file__).resolve().parent.parent
    db = Path(os.getenv("DB_PATH", str(root / "data" / "asa.db")))
    uploads = Path(os.getenv("UPLOAD_DIR", str(root / "data" / "uploads")))
    backups = Path(os.getenv("BACKUP_DIR", str(root / "data" / "backups")))
    return root, db, uploads, backups


def _print_backup_summary(result: BackupResult) -> None:
    m = result.manifest
    counts = m["database"].get("counts", {})
    print(
        "backup_ok",
        f"bundle={result.bundle_dir}",
        f"stamp={result.stamp}",
        f"appointments={counts.get('appointments', 0)}",
        f"patients={counts.get('patients', 0)}",
        f"migrations={counts.get('schema_migrations', 0)}",
        f"upload_files={m['uploads'].get('file_count', 0)}",
        f"db_sha256={m['database']['sha256'][:12]}…",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops.backup_core")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bak = sub.add_parser("backup", help="create verified backup bundle")
    p_bak.add_argument("--db", type=Path, default=None)
    p_bak.add_argument("--uploads", type=Path, default=None)
    p_bak.add_argument("--backup-dir", type=Path, default=None)
    p_bak.add_argument("--keep", type=int, default=int(os.getenv("BACKUP_KEEP", "14")))
    p_bak.add_argument(
        "--include-env",
        type=Path,
        default=None,
        help="optional env file to copy at mode 600 (contents never printed)",
    )

    p_ver = sub.add_parser("verify", help="verify an existing bundle")
    p_ver.add_argument("bundle", type=Path)

    p_res = sub.add_parser("restore", help="restore bundle into experimental directory")
    p_res.add_argument("bundle", type=Path)
    p_res.add_argument("dest_dir", type=Path, nargs="?", default=None)
    p_res.add_argument("--promote", action="store_true")
    p_res.add_argument("--live-db", type=Path, default=None)
    p_res.add_argument("--live-uploads", type=Path, default=None)

    p_leg = sub.add_parser("restore-legacy", help="restore old asa-*.db.gz (DB only)")
    p_leg.add_argument("archive", type=Path)
    p_leg.add_argument("dest_db", type=Path, nargs="?", default=None)
    p_leg.add_argument(
        "--confirm-live-overwrite",
        action="store_true",
        help="required when dest_db resolves to the live DB_PATH",
    )

    args = parser.parse_args(argv)
    root, db, uploads, backups = _default_paths()

    try:
        if args.cmd == "backup":
            result = create_backup(
                db_path=args.db or db,
                upload_dir=args.uploads or uploads,
                backup_dir=args.backup_dir or backups,
                repo_root=root,
                keep=args.keep,
                include_env=args.include_env,
            )
            _print_backup_summary(result)
            return 0
        if args.cmd == "verify":
            man = verify_bundle(args.bundle)
            print(
                "verify_ok",
                f"stamp={man.get('stamp')}",
                f"appointments={man['database']['counts'].get('appointments', 0)}",
                f"upload_files={man['uploads'].get('file_count', 0)}",
            )
            return 0
        if args.cmd == "restore":
            bundle = args.bundle
            if bundle.is_file() and bundle.name.endswith(".db.gz"):
                raise BackupError(
                    "legacy .db.gz detected — use: python -m ops.backup_core restore-legacy ..."
                )
            man = verify_bundle(bundle)
            dest = args.dest_dir
            if dest is None:
                dest = backups.parent / "restore-work" / str(man.get("stamp") or _utc_stamp())
            out = restore_bundle(bundle, dest)
            print(
                "restore_ok",
                f"dest={out['dest_dir']}",
                f"appointments={out['counts'].get('appointments', 0)}",
                f"patients={out['counts'].get('patients', 0)}",
                f"upload_files={out['upload_files']}",
            )
            if args.promote:
                promo = promote_restore(
                    Path(out["dest_dir"]),
                    live_db=args.live_db or db,
                    live_uploads=args.live_uploads or uploads,
                    bundle=bundle,
                )
                print("promote_ok", f"live_db={promo['live_db']}")
            return 0
        if args.cmd == "restore-legacy":
            dest = args.dest_db or db
            out = restore_legacy_db_gz(
                args.archive,
                dest,
                confirm_live_overwrite=args.confirm_live_overwrite,
                live_db=db,
            )
            print(
                "restore_legacy_ok",
                f"db={out['db']}",
                f"appointments={out['counts'].get('appointments', 0)}",
                f"bookings={out['counts'].get('bookings', 0)}",
            )
            return 0
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
