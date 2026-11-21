"""Patient identity: registration, returning-patient login, sessions, uploads.

A patient session is deliberately weaker than a staff session (6 h, no CSRF-protected
destructive actions) and can only ever reach that one patient's own rows — enforced by
`policies.py`, not by the templates.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import pathlib
import secrets
import zipfile

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

# MRI ZIP limits — injected into the private viewer so browser checks stay identical.
MRI_ZIP_MAX_ENTRIES = 1000
MRI_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024
MRI_ZIP_MAX_RATIO = 100  # compressed:uncompressed ceiling against zip bombs
_ZIP_ENCRYPT_FLAG = 0x1
_ZIP_UNIX_SYMLINK = 0o120000
_ZIP_ALLOWED_COMPRESS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def _zip_entry_path_ok(name: str) -> bool:
    path = name.replace("\\", "/")
    if not path or path.startswith("/") or path.startswith("../") or "/../" in f"/{path}/":
        return False
    if ".." in pathlib.PurePosixPath(path).parts:
        return False
    # Reject Windows drive paths (e.g. C:/...).
    return not (len(path) >= 2 and path[1] == ":" and path[0].isalpha())


def _zip_entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the high 16 bits of external_attr when create_system == 3.
    if info.create_system != 3:
        return False
    mode = (info.external_attr >> 16) & 0o170000
    return mode == _ZIP_UNIX_SYMLINK


def _validate_mri_zip(raw: bytes) -> None:
    """Reject Zip Slip, encryption, symlinks, bombs — using central-directory sizes only."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise BadRequest(
            "فایل ZIP معتبر نیست.",
            fields={"mri_file": "ZIP نامعتبر"},
        ) from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MRI_ZIP_MAX_ENTRIES:
            raise BadRequest(
                "تعداد فایل‌های داخل ZIP بیش از حد مجاز است.",
                fields={"mri_file": "ZIP بیش از حد شلوغ است"},
            )
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            if not _zip_entry_path_ok(name):
                raise BadRequest(
                    "مسیر فایل داخل ZIP مجاز نیست.",
                    fields={"mri_file": "مسیر ZIP نامعتبر"},
                )
            if info.flag_bits & _ZIP_ENCRYPT_FLAG:
                raise BadRequest(
                    "ZIP رمزگذاری‌شده پشتیبانی نمی‌شود.",
                    fields={"mri_file": "ZIP رمزگذاری‌شده"},
                )
            if _zip_entry_is_symlink(info):
                raise BadRequest(
                    "پیوند نمادین داخل ZIP مجاز نیست.",
                    fields={"mri_file": "symlink غیرمجاز"},
                )
            if info.compress_type not in _ZIP_ALLOWED_COMPRESS:
                raise BadRequest(
                    "روش فشرده‌سازی ZIP پشتیبانی نمی‌شود.",
                    fields={"mri_file": "compress غیرمجاز"},
                )
            # Central-directory sizes must be present and non-negative before any extract.
            if not isinstance(info.file_size, int) or not isinstance(info.compress_size, int):
                raise BadRequest(
                    "metadata فایل ZIP نامعتبر است.",
                    fields={"mri_file": "ZIP بدون اندازه معتبر"},
                )
            if info.file_size < 0 or info.compress_size < 0:
                raise BadRequest(
                    "فایل ZIP معتبر نیست.",
                    fields={"mri_file": "ZIP نامعتبر"},
                )
            if info.compress_size > 0 and info.file_size > info.compress_size * MRI_ZIP_MAX_RATIO:
                raise BadRequest(
                    "نسبت فشرده‌سازی ZIP بیش از حد مجاز است.",
                    fields={"mri_file": "ZIP مشکوک"},
                )
            total += info.file_size
            if total > MRI_ZIP_MAX_UNCOMPRESSED:
                raise BadRequest(
                    "حجم بازشده ZIP بیش از حد مجاز است.",
                    fields={"mri_file": "ZIP بیش از حد بزرگ است"},
                )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# uploads
# --------------------------------------------------------------------------
def save_bytes(raw: bytes, prefix: str, *, max_bytes: int | None = None) -> str:
    """Store validated private upload bytes using the same path as file uploads."""
    limit = settings.MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    if len(raw) > limit:
        raise PayloadTooLarge(f"حجم فایل بیشتر از {limit // (1024 * 1024)} مگابایت است.")
    if not raw:
        return ""
    # Detect supported formats from their actual signatures; never trust filenames.
    kind = None
    if raw.startswith(b"PK\x03\x04"):
        kind = "zip"
    elif len(raw) >= 132 and raw[128:132] == b"DICM":
        kind = "dicom"
    else:
        for magic, name in MAGIC.items():
            if raw.startswith(magic):
                kind = name
                break
    if kind == "webp" and raw[8:12] != b"WEBP":
        kind = None
    # DICOM/ZIP are accepted only for the MRI field, never for medication photos.
    if kind in {"dicom", "zip"} and not prefix.startswith("mri-"):
        kind = None
    if kind not in ALLOWED_IMAGE and kind not in {"pdf", "dicom", "zip"}:
        field = "mri_file" if prefix.startswith("mri-") else "med_photo"
        raise BadRequest(
            "فقط عکس (JPG، PNG، WEBP)، PDF، فایل DICOM یا ZIP حاوی تصاویر MRI قابل ارسال است.",
            fields={field: "نوع فایل مجاز نیست"},
        )
    if kind == "zip":
        _validate_mri_zip(raw)
    ext = {
        "jpeg": ".jpg",
        "png": ".png",
        "webp": ".webp",
        "pdf": ".pdf",
        "dicom": ".dcm",
        "zip": ".zip",
    }[kind]
    name = f"{prefix}-{hashlib.sha256(raw).hexdigest()[:16]}{ext}"
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (settings.UPLOAD_DIR / name).write_bytes(raw)
    # Never log filenames or DICOM tags — only kind + size fingerprint.
    info("upload.saved", kind=kind, bytes=len(raw))
    return name


def save_upload(file: UploadFile | None, prefix: str, *, max_bytes: int | None = None) -> str:
    """Store a private patient upload after sniffing its content and size."""
    if file is None or not file.filename:
        return ""
    limit = settings.MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    return save_bytes(file.file.read(limit + 1), prefix, max_bytes=limit)


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
def register(
    data: dict,
    *,
    med_photo: str,
    ip: str,
    mri_file: str = "",
    owner_id: int | None = None,
    enforce_consent: bool = True,
) -> dict:
    from . import legal_consent, ownership
    from .errors import Conflict, ValidationFailed

    version = ""
    accepted_at = ""
    text_hash = ""
    if enforce_consent:
        if not data.get("consent_accepted"):
            raise ValidationFailed(
                "پذیرش حریم خصوصی و قوانین استفاده الزامی است.",
                code="consent_required",
                fields={"consent_accepted": "پذیرش الزامی است."},
            )
        version = str(data.get("consent_version") or "").strip()
        if version != legal_consent.REGISTRATION_CONSENT_VERSION:
            raise ValidationFailed(
                "نسخه رضایت‌نامه معتبر نیست؛ صفحه را تازه کنید و دوباره تلاش کنید.",
                code="consent_version_mismatch",
                fields={"consent_version": "نسخه رضایت‌نامه معتبر نیست."},
            )
        accepted_at = _iso(_now())
        text_hash = legal_consent.consent_text_hash()

    existing = repo.select(
        SYSTEM, "patients", where="national_id = ?", params=[data["national_id"]], limit=1
    )
    if existing:
        # Not an error the patient should be punished for: send them to the login path.
        raise Conflict(
            "با این کد ملی قبلاً ثبت‌نام شده است. از گزینه‌ی «قبلاً ثبت‌نام کرده‌ام» وارد شوید.",
            code="already_registered",
        )
    # Single-clinic: attach to the sole active owner. Never create an unowned
    # patient via register — that would allow invisible bookings that occupy cabins.
    resolved_owner = owner_id
    if resolved_owner is None:
        resolved_owner = ownership.sole_active_owner_id()
    if resolved_owner is None:
        raise Conflict(
            "ثبت‌نام اینترنتی فعلاً ممکن نیست. لطفاً تلفنی با کلینیک هماهنگ کنید.",
            code="clinic_unavailable",
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
            "owner_id": resolved_owner,
            "consent_version": version,
            "consent_accepted_at": accepted_at,
            "consent_text_hash": text_hash,
        },
    )
    if enforce_consent:
        repo.insert(
            SYSTEM,
            "patient_consent_events",
            {
                "patient_id": pid,
                "consent_version": version,
                "consent_text_hash": text_hash,
                "accepted_at": accepted_at,
                "source": legal_consent.REGISTRATION_CONSENT_SOURCE,
                "ip_fp": fingerprint(ip),
            },
        )
    info(
        "patient.registered",
        patient_id=pid,
        owner_id=resolved_owner,
        consent_version=version or None,
        consent_enforced=enforce_consent,
    )
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
