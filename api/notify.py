"""Delivery of a booking to the clinic — the thing that makes the success toast true.

A booking is only reported as delivered once a provider confirmed it. Retries are
idempotent: the `deliveries` row is unique per (booking, provider).
"""

from __future__ import annotations

import datetime as dt
import json
import time

import httpx

from . import repo, tracking
from .config import settings
from .errors import DependencyFailed
from .logging_ import info, warn
from .policies import SYSTEM

SERVICE_FA = {
    "laser": "لیزر پرتوان",
    "shockwave": "شاک ویو",
    "tecar": "تکار تراپی",
    "manual": "درمان‌های دستی",
    "acupuncture": "طب سوزنی",
    "consult": "مشاوره رایگان",
    "rehab": "توانبخشی",
    "other": "سایر",
}


def _iso_now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render(booking: dict) -> str:
    return (
        "🩺 درخواست نوبت جدید — آسا فیزیو\n"
        f"نام: {booking['name']}\n"
        f"تلفن: {booking['phone']}\n"
        f"خدمت: {SERVICE_FA.get(booking['service'], booking['service'])}\n"
        f"توضیح: {booking['note'] or '—'}\n"
        f"کد پیگیری: {booking['public_id']}\n"
        f"زمان: {booking['created_at']}"
    )


def _send_file(booking: dict) -> None:
    settings.NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with settings.NOTIFY_FILE.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"at": _iso_now(), "text": render(booking), "booking": booking}, ensure_ascii=False
            )
            + "\n"
        )


def _send_webhook(booking: dict) -> None:
    if not settings.NOTIFY_WEBHOOK:
        raise RuntimeError("NOTIFY_WEBHOOK is empty")
    r = httpx.post(
        settings.NOTIFY_WEBHOOK, json={"text": render(booking), "booking": booking}, timeout=8.0
    )
    r.raise_for_status()


def _send_telegram(booking: dict) -> None:
    if not (settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_CHAT_ID):
        raise RuntimeError("telegram credentials are not configured")
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    r = httpx.post(
        url, json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": render(booking)}, timeout=8.0
    )
    r.raise_for_status()


PROVIDERS = {"file": _send_file, "webhook": _send_webhook, "telegram": _send_telegram}


def deliver(booking: dict, *, attempts: int = 3) -> bool:
    """Returns True only when the clinic really has the request."""
    provider = settings.NOTIFY_PROVIDER
    send = PROVIDERS.get(provider)
    if send is None:
        raise DependencyFailed(detail=f"unknown notify provider {provider!r}")

    with repo.tx() as conn:
        conn.execute(
            "INSERT INTO deliveries(booking_id, provider, status) VALUES (?,?, 'pending') "
            "ON CONFLICT(booking_id, provider) DO NOTHING",
            (booking["id"], provider),
        )
        row = conn.execute(
            "SELECT * FROM deliveries WHERE booking_id = ? AND provider = ?",
            (booking["id"], provider),
        ).fetchone()
    if row and row["status"] == "delivered":
        return True  # idempotent replay

    last_err = ""
    for i in range(attempts):
        try:
            send(booking)
            repo.update(
                SYSTEM,
                "deliveries",
                {
                    "status": "delivered",
                    "delivered_at": _iso_now(),
                    "attempts": (row["attempts"] if row else 0) + i + 1,
                    "updated_at": _iso_now(),
                    "last_error": "",
                },
                where="booking_id = ? AND provider = ?",
                params=[booking["id"], provider],
            )
            info("notify.delivered", booking_id=booking["id"], provider=provider, attempt=i + 1)
            return True
        except Exception as exc:  # noqa: BLE001 - provider failures are expected
            last_err = f"{type(exc).__name__}: {exc}"[:500]
            warn(
                "notify.attempt_failed",
                provider=provider,
                attempt=i + 1,
                exc_type=type(exc).__name__,
            )
            if i < attempts - 1:
                time.sleep(0.4 * (2**i))

    repo.update(
        SYSTEM,
        "deliveries",
        {
            "status": "failed",
            "attempts": (row["attempts"] if row else 0) + attempts,
            "last_error": last_err,
            "updated_at": _iso_now(),
        },
        where="booking_id = ? AND provider = ?",
        params=[booking["id"], provider],
    )
    tracking.notify(
        "booking_delivery_failed",
        "a booking could not be delivered to the clinic",
        booking_public_id=booking["public_id"],
        provider=provider,
    )
    return False


def retry_pending(limit: int = 20) -> dict[str, int]:
    rows = repo.select(
        SYSTEM,
        "deliveries",
        where="status = 'failed' AND attempts < 12",
        order_by="created_at",
        limit=limit,
    )
    ok = fail = 0
    for d in rows:
        b = repo.select(SYSTEM, "bookings", where="id = ?", params=[d["booking_id"]], limit=1)
        if not b:
            continue
        if deliver(b[0], attempts=1):
            ok += 1
            from . import metrics

            metrics.record("booking_delivered", booking_id=b[0]["id"])
        else:
            fail += 1
    return {"retried": len(rows), "delivered": ok, "failed": fail}
