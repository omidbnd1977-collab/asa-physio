"""Layer 02 — every error has an explicit type, a stable code and a trace id.

The wire envelope never changes shape:

    {"error": {"type": "...", "code": "...", "message": "...",
               "trace_id": "...", "fields": {...}, "retry_after": 12}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class. Anything not derived from this is an unhandled bug (layer 12)."""

    type: str = "internal_error"
    code: str = "internal_error"
    status: int = 500
    message: str = "خطای داخلی سرور"
    expose: bool = True  # may the message be shown to the end user?

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        fields: dict[str, str] | None = None,
        retry_after: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        if code:
            # a narrower machine-readable code inside the same type,
            # e.g. conflict/slot_full vs conflict/duplicate_booking
            self.code = code
        self.fields = fields or {}
        self.retry_after = retry_after
        self.detail = detail  # internal only, never serialised

    def payload(self, trace_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": self.type,
            "code": self.code,
            "message": self.message if self.expose else "خطای داخلی سرور",
            "trace_id": trace_id,
        }
        if self.fields:
            body["fields"] = self.fields
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return {"error": body}


class ValidationFailed(AppError):
    type, code, status = "validation_error", "invalid_input", 422
    message = "اطلاعات واردشده معتبر نیست."


class BadRequest(AppError):
    type, code, status = "bad_request", "bad_request", 400
    message = "درخواست نامعتبر است."


class Unauthorized(AppError):
    type, code, status = "unauthorized", "unauthorized", 401
    message = "برای این کار باید وارد شوید."


class Forbidden(AppError):
    type, code, status = "forbidden", "forbidden", 403
    message = "اجازه‌ی دسترسی به این بخش را ندارید."


class NotFound(AppError):
    type, code, status = "not_found", "not_found", 404
    message = "موردی پیدا نشد."


class Conflict(AppError):
    type, code, status = "conflict", "conflict", 409
    message = "این درخواست با وضعیت فعلی هم‌خوانی ندارد."


class IdempotencyMismatch(Conflict):
    code = "idempotency_key_reuse"
    message = "این کلید قبلاً با محتوای دیگری استفاده شده است."


class PayloadTooLarge(AppError):
    type, code, status = "payload_too_large", "payload_too_large", 413
    message = "حجم درخواست بیش از حد مجاز است."


class RateLimited(AppError):
    type, code, status = "rate_limited", "rate_limited", 429
    message = "تعداد درخواست‌ها بیش از حد مجاز است. کمی بعد دوباره تلاش کنید."


class CircuitOpen(AppError):
    type, code, status = "circuit_open", "circuit_open", 503
    message = "سرویس موقتاً در دسترس نیست. چند لحظه بعد دوباره تلاش کنید."


class BudgetExceeded(AppError):
    type, code, status = "budget_exceeded", "budget_exceeded", 503
    message = "سقف هزینه‌ی ماهانه‌ی این سرویس پر شده است."


class DependencyFailed(AppError):
    type, code, status = "dependency_failed", "dependency_failed", 502
    message = "ارسال درخواست به کلینیک انجام نشد. لطفاً تلفنی تماس بگیرید."


class PolicyMissing(AppError):
    """Layer 04 fail-closed guard: a table without an explicit policy is unreachable."""

    type, code, status = "policy_missing", "policy_missing", 500
    message = "خطای پیکربندی دسترسی"
    expose = False
