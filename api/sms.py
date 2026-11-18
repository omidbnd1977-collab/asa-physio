"""SMS delivery — every message is stored, costed and retryable.

Providers: `file` (development only), `webhook` (any gateway), `kavenegar`,
`smsir`. Layer 06 applies: each send is checked against the monthly cap *before*
it costs anything and recorded *after* it succeeds.
"""

from __future__ import annotations

import datetime as dt

import httpx

from . import budget, repo, tracking
from .config import settings
from .errors import BudgetExceeded
from .logging_ import fingerprint, info, warn
from .policies import SYSTEM

RESOURCE = "sms"
CLINIC = "آسا فیزیو"
# If a worker dies after claiming (status=sending), another may reclaim after this.
CLAIM_STALE_SEC = 120


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stale_before() -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=CLAIM_STALE_SEC)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# --------------------------------------------------------------------------
# message bodies — kept here so the wording is reviewed in one place
# --------------------------------------------------------------------------
def body_registered(name: str) -> str:
    return (
        f"{name} عزیز، اطلاعات شما در {CLINIC} ثبت شد.\n"
        "اکنون می‌توانید نوبت خود را انتخاب کنید.\n"
        "0902464 8159"
    )


def body_booked(name: str, when: str, code: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} ثبت شد.\n"
        f"زمان: {when}\n"
        f"کد پیگیری: {code}\n"
        "جزیره قشم، میدان ولایت، ساختمان محسنین، طبقه دوم"
    )


def body_rescheduled(name: str, old: str, new: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} تغییر کرد.\n"
        f"زمان قبلی: {old}\n"
        f"زمان جدید: {new}\n"
        "در صورت نیاز با ما تماس بگیرید: 0902464 8159"
    )


def body_cancelled(name: str, when: str) -> str:
    return (
        f"{name} عزیز، نوبت شما در {CLINIC} برای {when} لغو شد.\n"
        "برای هماهنگی مجدد تماس بگیرید: 0902464 8159"
    )


def body_followup(name: str, when: str) -> str:
    return f"{name} عزیز، جلسه بعدی فیزیوتراپی شما در {CLINIC} ثبت شد.\nزمان: {when}"


def body_welcome_back(name: str, when: str, gap_days: int) -> str:
    months = max(1, round(gap_days / 30))
    gap_fa = "یک ماه" if months == 1 else f"{months} ماه"
    return (
        f"{name} عزیز، از اینکه بعد از {gap_fa} دوباره {CLINIC} را انتخاب کردید "
        "سپاسگزاریم.\n"
        f"نوبت شما: {when}\n"
        "پرونده‌ی قبلی شما نزد ماست و درمان از همان‌جا ادامه پیدا می‌کند."
    )


def body_session_logged(name: str, when: str, treatments: str) -> str:
    return f"{name} عزیز، جلسه‌ی امروز شما در {CLINIC} ثبت شد.\nدرمان انجام‌شده: {treatments}\n{when}"


def body_confirm(name: str, when: str) -> str:
    return (
        f"{name} عزیز، یادآوری نوبت فیزیوتراپی {CLINIC}.\n"
        f"زمان: {when}\n"
        "برای تأیید حضور عدد ۱ و برای عدم حضور عدد ۲ را ارسال کنید."
    )


def body_custom(name: str, text: str) -> str:
    """A reply the doctor typed in the panel, forwarded to the patient's phone."""
    body = text.strip()[:520]
    return f"{name} عزیز، پیام {CLINIC}:\n{body}"


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
def _send_file(phone: str, body: str) -> None:
    settings.SMS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with settings.SMS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"=== {_now()}  ->  {phone}\n{body}\n\n")


def _send_webhook(phone: str, body: str) -> None:
    if not settings.SMS_WEBHOOK:
        raise RuntimeError("SMS_WEBHOOK is empty")
    r = httpx.post(
        settings.SMS_WEBHOOK,
        json={"to": phone, "text": body},
        headers={"Authorization": f"Bearer {settings.SMS_API_KEY}"} if settings.SMS_API_KEY else {},
        timeout=10.0,
    )
    r.raise_for_status()


def _send_kavenegar(phone: str, body: str) -> None:
    if not settings.SMS_API_KEY:
        raise RuntimeError("SMS_API_KEY is empty")
    url = f"https://api.kavenegar.com/v1/{settings.SMS_API_KEY}/sms/send.json"
    r = httpx.post(
        url, data={"receptor": phone, "message": body, "sender": settings.SMS_SENDER}, timeout=10.0
    )
    r.raise_for_status()
    payload = r.json()
    status = payload.get("return", {}).get("status")
    if status != 200:
        raise RuntimeError(f"kavenegar status {status}")


def normalize_mobile(phone: str) -> str:
    """Normalize Iranian mobiles to ``09xxxxxxxxx`` for SMS.ir bulk payloads."""
    p = phone.strip().replace(" ", "").replace("-", "")
    if p.startswith("+98"):
        p = "0" + p[3:]
    elif p.startswith("0098"):
        p = "0" + p[4:]
    elif p.startswith("98") and len(p) >= 12:
        p = "0" + p[2:]
    return p


def _smsir_line_number() -> int:
    raw = (settings.SMS_SENDER or "").strip()
    if not raw:
        raise RuntimeError("SMS_SENDER (sms.ir lineNumber) is empty")
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError("SMS_SENDER must be a numeric sms.ir lineNumber") from exc


# Official SMS.ir Verify parameter values are capped at 25 characters.
VERIFY_PARAM_MAX_LEN = 25

# Panel template IDs (Issue #27). Names must match the panel keys without #.
SMSIR_TEMPLATE_ADMIN_NEW = 753761  # FULLNAME PHONE DATE TIME CODE → manager
SMSIR_TEMPLATE_BOOKED = 371089  # FULLNAME DATE TIME CODE → patient
SMSIR_TEMPLATE_CONFIRM = 666137  # FULLNAME DATE TIME → patient (reply 1/2)
SMSIR_TEMPLATE_STATUS = 730888  # FULLNAME → patient (status change)

VERIFY_TEMPLATES: dict[str, int] = {
    "admin_appointment": SMSIR_TEMPLATE_ADMIN_NEW,
    "booked": SMSIR_TEMPLATE_BOOKED,
    "confirm_request": SMSIR_TEMPLATE_CONFIRM,
    "cancelled": SMSIR_TEMPLATE_STATUS,
    "rescheduled": SMSIR_TEMPLATE_STATUS,
}

# Patient-facing kinds: hard-denied for live SMS.ir (ENV cannot enable them).
PATIENT_SMS_KINDS: frozenset[str] = frozenset(
    {"booked", "confirm_request", "cancelled", "rescheduled"}
)

# Absolute ceiling for live SMS.ir via the queue/deliver path. ENV may only
# subset this set; today that means admin_appointment alone.
SMSIR_HARD_ALLOWED_KINDS: frozenset[str] = frozenset({"admin_appointment"})

# Legacy clinic-booking notify channel (notify._send_smsir). Not a patient kind;
# Phase C may fail-close this separately via ALLOW_NOTIFY_SMSIR.
NOTIFY_BOOKING_ADMIN_KIND = "notify_booking_admin"


def sms_ir_allowed_kinds() -> frozenset[str]:
    """Kinds permitted to use the live SMS.ir provider.

    Default empty ⇒ fail-closed (no real SMS.ir traffic) even if SMS_PROVIDER=smsir.
    Patient kinds are stripped even if present in ``SMS_IR_ALLOWED_KINDS``.
    Result is always intersected with ``SMSIR_HARD_ALLOWED_KINDS``.
    """
    raw = (settings.SMS_IR_ALLOWED_KINDS or "").strip()
    if not raw:
        return frozenset()
    requested = frozenset(part.strip() for part in raw.split(",") if part.strip())
    return (requested - PATIENT_SMS_KINDS) & SMSIR_HARD_ALLOWED_KINDS


def kind_may_use_smsir(kind: str | None) -> bool:
    """Server-side gate: allowlisted ∧ hard-allowed ∧ not a patient kind."""
    if not kind or kind in PATIENT_SMS_KINDS:
        return False
    if kind not in SMSIR_HARD_ALLOWED_KINDS:
        return False
    return kind in sms_ir_allowed_kinds()


def assert_smsir_send_kind_allowed(kind: str | None) -> str:
    """Central gate for every live SMS.ir HTTP call (queue or direct).

    Patient kinds are always rejected. Queue kinds must pass ``kind_may_use_smsir``.
    ``notify_booking_admin`` is the only non-queue exception (legacy notify path).
    """
    if not kind or not str(kind).strip():
        raise RuntimeError("sms.ir send requires kind=")
    normalized = str(kind).strip()
    if normalized in PATIENT_SMS_KINDS:
        raise RuntimeError(f"sms.ir kind {normalized!r} is hard-denied (patient)")
    if normalized in SMSIR_HARD_ALLOWED_KINDS:
        if not kind_may_use_smsir(normalized):
            raise RuntimeError(f"sms.ir kind {normalized!r} is not allowlisted")
        return normalized
    if normalized == NOTIFY_BOOKING_ADMIN_KIND:
        return normalized
    raise RuntimeError(f"sms.ir kind {normalized!r} is not permitted")


def verify_param(name: str, value: str) -> dict[str, str]:
    """Build one Verify parameter; refuse empty or overlong values (no silent trim)."""
    key = name.strip().strip("#")
    if not key:
        raise RuntimeError("sms.ir verify parameter name is empty")
    text = (value or "").strip()
    if not text:
        raise RuntimeError(f"sms.ir verify parameter {key} is empty")
    if len(text) > VERIFY_PARAM_MAX_LEN:
        raise RuntimeError(
            f"sms.ir verify parameter {key} exceeds {VERIFY_PARAM_MAX_LEN} characters"
        )
    return {"name": key, "value": text}


def appointment_date_time(appt: dict) -> tuple[str, str]:
    """Compact Jalali date + slot time for template DATE/TIME (fits 25-char limit)."""
    from .jalali import to_jalali_str

    return to_jalali_str(appt["slot_date"]), str(appt["slot_time"]).strip()


def _load_appointment(appointment_id: int | None) -> dict | None:
    if not appointment_id:
        return None
    rows = repo.select(SYSTEM, "appointments", where="id = ?", params=[appointment_id], limit=1)
    return rows[0] if rows else None


def _load_patient(patient_id: int | None) -> dict | None:
    if not patient_id:
        return None
    rows = repo.select(SYSTEM, "patients", where="id = ?", params=[patient_id], limit=1)
    return rows[0] if rows else None


def build_verify_parameters(kind: str, *, patient: dict, appt: dict | None) -> list[dict[str, str]]:
    """Map queued SMS kinds to SMS.ir Verify parameter lists."""
    name = str(patient.get("full_name") or "").strip()
    if kind == "admin_appointment":
        if not appt:
            raise RuntimeError("admin_appointment verify requires appointment row")
        date_s, time_s = appointment_date_time(appt)
        patient_phone = normalize_mobile(str(patient.get("phone") or ""))
        return [
            verify_param("FULLNAME", name),
            verify_param("PHONE", patient_phone),
            verify_param("DATE", date_s),
            verify_param("TIME", time_s),
            verify_param("CODE", str(appt.get("public_id") or "")),
        ]
    if kind == "booked":
        if not appt:
            raise RuntimeError("booked verify requires appointment row")
        date_s, time_s = appointment_date_time(appt)
        return [
            verify_param("FULLNAME", name),
            verify_param("DATE", date_s),
            verify_param("TIME", time_s),
            verify_param("CODE", str(appt.get("public_id") or "")),
        ]
    if kind == "confirm_request":
        if not appt:
            raise RuntimeError("confirm_request verify requires appointment row")
        date_s, time_s = appointment_date_time(appt)
        return [
            verify_param("FULLNAME", name),
            verify_param("DATE", date_s),
            verify_param("TIME", time_s),
        ]
    if kind in {"cancelled", "rescheduled"}:
        return [verify_param("FULLNAME", name)]
    raise RuntimeError(f"no SMS.ir verify mapping for kind {kind!r}")


def verify_payload_for_message(msg: dict) -> tuple[int, list[dict[str, str]]] | None:
    """Return (templateId, parameters) for templated kinds, else None → bulk text."""
    kind = msg.get("kind") or ""
    template_id = VERIFY_TEMPLATES.get(kind)
    if template_id is None:
        return None
    appt = _load_appointment(msg.get("appointment_id"))
    patient = _load_patient(msg.get("patient_id"))
    if patient is None and appt is not None:
        patient = _load_patient(appt.get("patient_id"))
    if patient is None:
        raise RuntimeError(f"sms.ir verify for {kind} needs patient row")
    return template_id, build_verify_parameters(kind, patient=patient, appt=appt)


def _smsir_headers() -> dict[str, str]:
    if not settings.SMS_API_KEY:
        raise RuntimeError("SMS_API_KEY is empty")
    return {
        "X-API-KEY": settings.SMS_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def send_smsir_verify(
    phone: str,
    template_id: int,
    parameters: list[dict[str, str]],
    *,
    kind: str,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> int | None:
    """Official SMS.ir Verify send: POST https://api.sms.ir/v1/send/verify

    Auth: ``X-API-KEY``. Body: mobile, templateId (int), parameters[{name,value}].
    Parameter values max 25 chars. Success when HTTP 200 and JSON ``status == 1``.
    Returns provider messageId when present; never logs the API key.
    Requires ``kind=``; patient kinds cannot bypass the central hard-deny gate.
    Docs: https://sms.ir/rest-api/
    """
    kind = assert_smsir_send_kind_allowed(kind)
    if kind == "admin_appointment":
        _assert_admin_appointment_destination(
            {
                "kind": kind,
                "phone": phone,
                "patient_id": patient_id,
                "appointment_id": appointment_id,
            }
        )
    mobile = normalize_mobile(phone)
    if not mobile:
        raise RuntimeError("SMS recipient phone is empty")
    if not isinstance(template_id, int) or template_id <= 0:
        raise RuntimeError("sms.ir templateId must be a positive integer")
    if not parameters:
        raise RuntimeError("sms.ir verify parameters are empty")
    # Re-validate every value before the HTTP call.
    safe_params = [verify_param(p["name"], p["value"]) for p in parameters]
    r = httpx.post(
        "https://api.sms.ir/v1/send/verify",
        headers=_smsir_headers(),
        json={
            "mobile": mobile,
            "templateId": template_id,
            "parameters": safe_params,
        },
        timeout=10.0,
    )
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError as exc:
        raise RuntimeError("sms.ir verify returned non-JSON body") from exc
    status = payload.get("status")
    if status != 1:
        raise RuntimeError(f"sms.ir verify status {status}: {payload.get('message') or 'error'}")
    data = payload.get("data") or {}
    message_id = data.get("messageId")
    info(
        "sms.verify_sent",
        template_id=template_id,
        to=fingerprint(mobile),
        message_id=message_id if isinstance(message_id, int) else None,
    )
    return message_id if isinstance(message_id, int) else None


def send_smsir(
    phone: str,
    body: str,
    *,
    kind: str,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> None:
    """Official SMS.ir REST bulk send: POST https://api.sms.ir/v1/send/bulk

    Auth: ``X-API-KEY`` header. Body: lineNumber (long), messageText, mobiles[].
    Success when HTTP 200 and JSON ``status == 1``. messageIds entry ``0`` means
    blacklist; ``null`` means invalid number / oversized text.
    Requires ``kind=``; patient kinds cannot bypass the central hard-deny gate.
    Docs: https://sms.ir/rest-api/
    """
    kind = assert_smsir_send_kind_allowed(kind)
    if kind == "admin_appointment":
        _assert_admin_appointment_destination(
            {
                "kind": kind,
                "phone": phone,
                "patient_id": patient_id,
                "appointment_id": appointment_id,
            }
        )
    mobile = normalize_mobile(phone)
    if not mobile:
        raise RuntimeError("SMS recipient phone is empty")
    r = httpx.post(
        "https://api.sms.ir/v1/send/bulk",
        headers=_smsir_headers(),
        json={
            "lineNumber": _smsir_line_number(),
            "messageText": body,
            "mobiles": [mobile],
        },
        timeout=10.0,
    )
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError as exc:
        raise RuntimeError("sms.ir returned non-JSON body") from exc
    status = payload.get("status")
    if status != 1:
        raise RuntimeError(f"sms.ir status {status}: {payload.get('message') or 'error'}")
    data = payload.get("data") or {}
    ids = data.get("messageIds")
    if isinstance(ids, list) and ids:
        first = ids[0]
        if first == 0:
            raise RuntimeError("sms.ir rejected recipient (blacklist)")
        if first is None:
            raise RuntimeError("sms.ir rejected recipient (invalid number or text)")


def _assert_admin_appointment_destination(msg: dict) -> None:
    """Hard gate: admin_appointment may only go to ADMIN_NOTIFICATION_PHONE."""
    admin = normalize_mobile(settings.ADMIN_NOTIFICATION_PHONE or "")
    if not admin:
        raise RuntimeError("ADMIN_NOTIFICATION_PHONE is empty")
    dest = normalize_mobile(str(msg.get("phone") or ""))
    if dest != admin:
        raise RuntimeError("admin_appointment recipient is not ADMIN_NOTIFICATION_PHONE")
    patient = _load_patient(msg.get("patient_id"))
    if patient is None:
        appt = _load_appointment(msg.get("appointment_id"))
        if appt is not None:
            patient = _load_patient(appt.get("patient_id"))
    if patient:
        patient_phone = normalize_mobile(str(patient.get("phone") or ""))
        if patient_phone and patient_phone == admin:
            raise RuntimeError("ADMIN_NOTIFICATION_PHONE must not equal the patient phone")


def send_smsir_for_message(msg: dict) -> None:
    """Deliver one queued row via Verify when mapped, otherwise bulk text.

    Templated kinds must not silently fall back to free-text bulk — incomplete
    patient/appointment rows fail the delivery attempt so retry can surface the error.
    """
    kind = msg.get("kind") or ""
    if not kind_may_use_smsir(kind):
        raise RuntimeError(f"sms.ir kind {kind!r} is not allowlisted")
    if kind == "admin_appointment":
        _assert_admin_appointment_destination(msg)
    if kind in VERIFY_TEMPLATES:
        built = verify_payload_for_message(msg)
        if built is None:
            raise RuntimeError(f"sms.ir verify mapping missing for kind {kind!r}")
        template_id, parameters = built
        send_smsir_verify(
            msg["phone"],
            template_id,
            parameters,
            kind=kind,
            patient_id=msg.get("patient_id"),
            appointment_id=msg.get("appointment_id"),
        )
        return
    send_smsir(
        msg["phone"],
        msg["body"],
        kind=kind,
        patient_id=msg.get("patient_id"),
        appointment_id=msg.get("appointment_id"),
    )


def _smsir_providers_entry_blocked(phone: str, body: str) -> None:
    """PROVIDERS['smsir'] must not offer an unguarded (phone, body) send."""
    raise RuntimeError(
        "SMS_PROVIDER=smsir cannot send via PROVIDERS entry; "
        "use send_smsir(..., kind=...) or sms.deliver()"
    )


PROVIDERS = {
    "file": _send_file,
    "webhook": _send_webhook,
    "kavenegar": _send_kavenegar,
    "smsir": _smsir_providers_entry_blocked,
}


def dispatch(phone: str, body: str, *, kind: str) -> None:
    """Direct send via ``SMS_PROVIDER`` — still centrally gated for smsir.

    Deprecated for new call sites; prefer ``queue``/``deliver``. Requires ``kind=``.
    """
    if settings.SMS_PROVIDER == "smsir":
        send_smsir(phone, body, kind=kind)
        return
    send = PROVIDERS.get(settings.SMS_PROVIDER)
    if send is None:
        raise RuntimeError(f"unknown SMS provider {settings.SMS_PROVIDER!r}")
    send(phone, body)


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------
def queue(
    phone: str,
    body: str,
    kind: str,
    *,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> int:
    now = _now()
    return repo.insert(
        SYSTEM,
        "sms_messages",
        {
            "patient_id": patient_id,
            "appointment_id": appointment_id,
            "phone": phone,
            "kind": kind,
            "body": body[:600],
            "provider": settings.SMS_PROVIDER,
            "updated_at": now,
        },
    )


def ensure_admin_appointment(
    phone: str,
    body: str,
    appointment_id: int,
) -> int:
    """Insert one pending admin_appointment row, or return the existing id.

    Relies on ``idx_sms_admin_appointment_unique`` + ``ON CONFLICT DO NOTHING``.
    """
    now = _now()
    with repo.tx() as conn:
        conn.execute(
            "INSERT INTO sms_messages("
            " patient_id, appointment_id, phone, kind, body, status, provider,"
            " attempts, last_error, sent_at, created_at, updated_at)"
            " VALUES (NULL, ?, ?, 'admin_appointment', ?, 'pending', ?, 0, '', NULL, ?, ?)"
            " ON CONFLICT(appointment_id)"
            " WHERE kind = 'admin_appointment' AND appointment_id IS NOT NULL"
            " DO NOTHING",
            (appointment_id, phone, body[:600], settings.SMS_PROVIDER, now, now),
        )
        row = conn.execute(
            "SELECT id FROM sms_messages WHERE appointment_id = ? AND kind = 'admin_appointment'",
            (appointment_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("admin_appointment row missing after upsert")
    return int(row["id"])


def claim_for_send(message_id: int) -> dict | None:
    """Atomically take a pending/failed (or stale sending) row for delivery.

    Returns the claimed row, or ``None`` if another worker holds a fresh claim
    or the message is already ``sent``.
    """
    now = _now()
    stale = _stale_before()
    with repo.tx() as conn:
        cur = conn.execute(
            "UPDATE sms_messages"
            " SET status = 'sending', updated_at = ?, last_error = ''"
            " WHERE id = ? AND ("
            "   status IN ('pending', 'failed')"
            "   OR (status = 'sending' AND updated_at < ?)"
            " )",
            (now, message_id, stale),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM sms_messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def deliver(message_id: int, *, attempts: int = 2) -> bool:
    rows = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[message_id], limit=1)
    if not rows:
        return False
    if rows[0]["status"] == "sent":
        return True

    msg = claim_for_send(message_id)
    if msg is None:
        # Another worker is sending, or already finished — never send twice.
        again = repo.select(SYSTEM, "sms_messages", where="id = ?", params=[message_id], limit=1)
        return bool(again and again[0]["status"] == "sent")

    send = PROVIDERS.get(settings.SMS_PROVIDER)
    if send is None:
        repo.update(
            SYSTEM,
            "sms_messages",
            {
                "status": "failed",
                "last_error": f"unknown provider {settings.SMS_PROVIDER!r}",
                "updated_at": _now(),
            },
            where="id = ?",
            params=[message_id],
        )
        return False

    # layer 06 — never spend past the monthly cap
    try:
        budget.guard(RESOURCE, 1.0)
    except BudgetExceeded as exc:
        repo.update(
            SYSTEM,
            "sms_messages",
            {"status": "failed", "last_error": str(exc)[:400], "updated_at": _now()},
            where="id = ?",
            params=[message_id],
        )
        tracking.notify(
            "sms_budget_blocked", "an SMS was not sent: cap reached", sms_kind=msg["kind"]
        )
        return False

    last_err = ""
    kind = msg.get("kind") or ""
    use_smsir = settings.SMS_PROVIDER == "smsir" and kind_may_use_smsir(kind)
    # Non-allowlisted kinds never touch SMS.ir — force the local file sink.
    effective_provider = (
        "smsir"
        if use_smsir
        else ("file" if settings.SMS_PROVIDER == "smsir" else settings.SMS_PROVIDER)
    )
    for i in range(attempts):
        try:
            if use_smsir:
                send_smsir_for_message(msg)
            elif settings.SMS_PROVIDER == "smsir":
                _send_file(msg["phone"], msg["body"])
            else:
                send(msg["phone"], msg["body"])
            budget.record(RESOURCE, 1.0)
            repo.update(
                SYSTEM,
                "sms_messages",
                {
                    "status": "sent",
                    "sent_at": _now(),
                    "attempts": msg["attempts"] + i + 1,
                    "last_error": "",
                    "provider": effective_provider,
                    "updated_at": _now(),
                },
                where="id = ?",
                params=[message_id],
            )
            info(
                "sms.sent",
                sms_kind=kind,
                provider=effective_provider,
                to=fingerprint(msg["phone"]),
                via_verify=bool(use_smsir and kind in VERIFY_TEMPLATES),
                smsir_allowlisted=use_smsir,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — gateway failures are expected
            last_err = f"{type(exc).__name__}: {exc}"[:400]
            warn("sms.attempt_failed", kind=kind, attempt=i + 1, exc_type=type(exc).__name__)

    repo.update(
        SYSTEM,
        "sms_messages",
        {
            "status": "failed",
            "attempts": msg["attempts"] + attempts,
            "last_error": last_err,
            "updated_at": _now(),
        },
        where="id = ?",
        params=[message_id],
    )
    tracking.notify(
        "sms_delivery_failed",
        "an SMS could not be delivered",
        sms_kind=msg["kind"],
        error=last_err[:120],
    )
    return False


def send_now(
    phone: str,
    body: str,
    kind: str,
    *,
    patient_id: int | None = None,
    appointment_id: int | None = None,
) -> tuple[int, bool]:
    mid = queue(phone, body, kind, patient_id=patient_id, appointment_id=appointment_id)
    return mid, deliver(mid)


def retry_failed(limit: int = 20) -> dict[str, int]:
    stale = _stale_before()
    rows = repo.select(
        SYSTEM,
        "sms_messages",
        where=("(status = 'failed' OR (status = 'sending' AND updated_at < ?)) AND attempts < 10"),
        params=[stale],
        order_by="created_at",
        limit=limit,
    )
    ok = sum(1 for r in rows if deliver(r["id"], attempts=1))
    return {"retried": len(rows), "sent": ok, "failed": len(rows) - ok}
