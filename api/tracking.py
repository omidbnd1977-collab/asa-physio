"""Layer 12 — unhandled errors reach a human, with a trace id, automatically."""

from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any

import httpx

from .config import settings
from .logging_ import error, scrub

_seen: dict[str, float] = {}
_lock = threading.Lock()
DEDUPE_S = 300


def _should_send(key: str) -> bool:
    now = time.time()
    with _lock:
        for k, t in list(_seen.items()):
            if now - t > DEDUPE_S:
                _seen.pop(k, None)
        if key in _seen:
            return False
        _seen[key] = now
        return True


def _sink(payload: dict[str, Any]) -> None:
    """Webhook when configured, append-only file otherwise. Never raises."""
    line = json.dumps(payload, ensure_ascii=False)
    try:
        settings.ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with settings.ALERT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: S110 - pragma: no cover; alerting must never break a request
        pass
    if settings.ALERT_WEBHOOK:
        try:
            httpx.post(settings.ALERT_WEBHOOK, json=payload, timeout=4.0)
        except Exception:  # noqa: S110 - pragma: no cover; best-effort webhook
            pass


def capture(exc: BaseException, *, trace_id: str, where: str, **context: Any) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    key = f"{type(exc).__name__}:{where}:{tb.strip().splitlines()[-1][:120] if tb else ''}"
    payload = {
        "kind": "unhandled_error",
        "env": settings.ENV,
        "version": settings.VERSION,
        "trace_id": trace_id,
        "where": where,
        "exc_type": type(exc).__name__,
        "exc_msg": scrub(str(exc))[:300],
        "traceback": scrub(tb)[-4000:],
        "context": scrub(context),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    error("unhandled_error", where=where, exc_type=type(exc).__name__)
    if _should_send(key):
        threading.Thread(target=_sink, args=(payload,), daemon=True).start()


def notify(kind: str, message: str, /, **context: Any) -> None:
    """Operational alert that is not an exception (budget cap, breaker, delivery failure)."""
    payload = {
        "kind": kind,
        "env": settings.ENV,
        "version": settings.VERSION,
        "message": scrub(message),
        "context": scrub(context),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if _should_send(f"{kind}:{message[:80]}"):
        threading.Thread(target=_sink, args=(payload,), daemon=True).start()
