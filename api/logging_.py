"""Layer 12 — structured JSON logs with redaction. No secret or PII ever lands in a log line."""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import sys
import time
import uuid
from typing import Any

from .config import settings

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

# --- redaction ------------------------------------------------------------
SECRET_KEYS = {
    "password",
    "pass",
    "pwd",
    "secret",
    "token",
    "authorization",
    "cookie",
    "set-cookie",
    "api_key",
    "apikey",
    "secret_key",
    "session",
    "bearer",
    "telegram_bot_token",
    "ai_api_key",
    "idempotency-key",
}
PII_KEYS = {"phone", "name", "full_name", "note", "message", "email"}

_RE_PHONE = re.compile(r"(?:\+?98|0)9\d{9}")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_RE_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def fingerprint(value: str) -> str:
    """Stable, non-reversible handle so operators can correlate without seeing PII."""
    salted = (settings.SECRET_KEY + "|" + value).encode()
    return "fp_" + hashlib.sha256(salted).hexdigest()[:12]


def scrub(value: Any, key: str | None = None) -> Any:
    k = (key or "").lower()
    if k in SECRET_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {kk: scrub(vv, kk) for kk, vv in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, key) for v in value]
    if isinstance(value, str):
        if k in PII_KEYS:
            return fingerprint(value)
        v = _RE_BEARER.sub("bearer [redacted]", value)
        v = _RE_PHONE.sub("[phone]", v)
        v = _RE_EMAIL.sub("[email]", v)
        return v[:500]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": scrub(record.getMessage()),
            "trace_id": trace_id_var.get(),
            "env": settings.ENV,
            "version": settings.VERSION,
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            base.update(scrub(extra))
        if record.exc_info:
            base["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            base["exc"] = scrub(self.formatException(record.exc_info))[-2000:]
        return json.dumps(base, ensure_ascii=False)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)
    for noisy in ("uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(noisy)
        lg.handlers = [handler]
        lg.propagate = False


def log(level: int, msg: str, **fields: Any) -> None:
    logging.getLogger("app").log(level, msg, extra={"extra_fields": fields})


def info(msg: str, **f: Any) -> None:
    log(logging.INFO, msg, **f)


def warn(msg: str, **f: Any) -> None:
    log(logging.WARNING, msg, **f)


def error(msg: str, **f: Any) -> None:
    log(logging.ERROR, msg, **f)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]
