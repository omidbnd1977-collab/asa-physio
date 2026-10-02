"""Patient identity: registration, returning-patient login, sessions, uploads.

A patient session is deliberately weaker than a staff session (6 h, no CSRF-protected
destructive actions) and can only ever reach that one patient's own rows — enforced by
`policies.py`, not by the templates.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import pathlib
import secrets

from fastapi import UploadFile

from . import repo
from .config import settings
from .errors import BadRequest, PayloadTooLarge, Unauthorized
from .jalali import to_jalali_str
from .logging_ import fingerprint, info, warn
from .policies import SYSTEM, Actor

PATIENT_COOKIE = "asa_patient"

ALLOWED_IMAGE = {"jpeg", "png", "webp"}
MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"RIFF": "webp",
    b"%PDF-": "pdf",
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------
def save_upload(file: UploadFile | None, prefix: str) -> str:
    """Store a medication photo. Content is sniffed, the client's name is never trusted."""
    if file is None or not file.filename:
        return ""
    raw = file.file.read(settings.MAX_UPLOAD_BYTES + 1)
    if len(raw) > settings.MAX_UPLOAD_BYTES:
        raise PayloadTooLarge(
            f"حجم فایل بیشتر از {settings.MAX_UPLOAD_BYTES // (1024 * 1024)} مگابایت است."
        )
    if not raw:
        return ""

    # Sniff the real content. `imghdr` was removed in Python 3.13 and the client's
    # filename is attacker-controlled, so magic bytes are the only thing we trust.
    kind = None
    for magic, name in MAGIC.items():
        if raw.startswith(magic):
            kind = name
            break
    if kind == "webp" and raw[8:12] != b"WEBP":
        kind = None
    if kind not in ALLOWED_IMAGE and kind != "pdf":
        raise BadRequest(
            "فقط عکس (JPG، PNG، WEBP) یا فایل PDF قابل ارسال است.",
            fields={"med_photo": "نوع فایل مجاز نیست"},
        )

    ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "pdf": ".pdf"}[kind]
    name = f"{prefix}-{hashlib.sha256(raw).hexdigest()[:16]}{ext}"
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (settings.UPLOAD_DIR / name).write_bytes(raw)
    info("upload.saved", kind=kind, bytes=len(raw))
    return name


def upload_path(name: str) -> pathlib.Path | None:
    """Resolve a stored upload, refusing anything that escapes the directory."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    root = settings.UPLOAD_DIR.resolve()
    p = (root / name).resolve()
    if not str(p).startswith(str(root)) or not p.is_file():
        return None
    return p


# --------------------------------------------------------------------------
# registration and login
# --------------------------------------------------------------------------
def register(data: dict, *, med_photo: str, ip: str, mri_file: str = "") -> dict:
    existing = repo.select(
        SYSTEM, "patients", where="national_id = ?", params=[data["national_id"]], limit=1
    )
    if existing:
        # Not an error the patient should be punished for: send them to the login path.
        from .errors import Conflict

        raise Conflict(
            "با این کد ملی قبلاً ثبت‌نام شده است. از گزینه‌ی «قبلاً ثبت‌نام کرده‌ام» وارد شوید.",
            code="already_registered",
        )
    public_id = secrets.token_hex(6)
    pid = repo.insert(
        SYSTEM,
        "patients",
        {
            "public_id": public_id,
            "national_id": data["national_id"],
            "full_name": data["full_name"],
            "birth_date": data["birth_date"],
            "birth_jalali": to_jalali_str(data["birth_date"]),
            "phone": data["phone"],
            "mri_link": data.get("mri_link", ""),
            "ortho_doctor": data.get("ortho_doctor", ""),
            "med_photo": med_photo,
            "mri_file": mri_file,
            "ip_fp": fingerprint(ip),
        },
    )
    info("patient.registered", patient_id=pid)
    return repo.select(SYSTEM, "patients", where="id = ?", params=[pid], limit=1)[0]


def login(national_id: str, birth_date: str) -> dict:
    rows = repo.select(
        SYSTEM,
        "patients",
        where="national_id = ? AND birth_date = ? AND is_blocked = 0",
        params=[national_id, birth_date],
        limit=1,
    )
    if not rows:
        warn("patient.login_failed", nid=fingerprint(national_id))
        raise Unauthorized(
            "کد ملی یا تاریخ تولد با اطلاعات ثبت‌نام مطابقت ندارد. "
            "اگر بار اول است، ابتدا ثبت‌نام کنید."
        )
    info("patient.login_ok", patient_id=rows[0]["id"])
    return rows[0]


def start_session(patient_id: int, ip: str) -> tuple[str, dt.datetime]:
    sid = secrets.token_hex(32)
    expires = _now() + dt.timedelta(hours=settings.PATIENT_SESSION_TTL_H)
    repo.insert(
        SYSTEM,
        "patient_sessions",
        {
            "id": sid,
            "patient_id": patient_id,
            "expires_at": _iso(expires),
            "ip_fp": fingerprint(ip),
        },
    )
    return sid, expires


def resolve(sid: str | None) -> dict | None:
    if not sid or len(sid) != 64:
        return None
    rows = repo.select(SYSTEM, "patient_sessions", where="id = ?", params=[sid], limit=1)
    if not rows:
        return None
    s = rows[0]
    if s["revoked_at"] or s["expires_at"] <= _iso(_now()):
        return None
    p = repo.select(
        SYSTEM, "patients", where="id = ? AND is_blocked = 0", params=[s["patient_id"]], limit=1
    )
    return p[0] if p else None


def revoke(sid: str) -> None:
    repo.update(
        SYSTEM, "patient_sessions", {"revoked_at": _iso(_now())}, where="id = ?", params=[sid]
    )


def require(patient: dict | None) -> dict:
    if not patient:
        raise Unauthorized("برای ادامه ابتدا وارد شوید.")
    return patient


def patient_actor(patient: dict) -> Actor:
    """A patient is never a staff role; data access still goes through policies."""
    return Actor(role="anon", user_id=None)


def public_view(p: dict) -> dict:
    return {
        "public_id": p["public_id"],
        "full_name": p["full_name"],
        "phone": p["phone"][:4] + "***" + p["phone"][-2:],
        "birth_jalali": p["birth_jalali"],
    }
