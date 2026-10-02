"""The one paid / AI code path, wrapped in every guard layers 06 and 09 demand.

Staff-only, per-user limit, global daily limit, circuit breaker, budget cap checked
*before* the call, cost recorded *after* it, and a deterministic offline fallback so the
feature degrades instead of failing.
"""

from __future__ import annotations

import httpx

from . import budget, ratelimit, tracking
from .config import settings
from .errors import DependencyFailed, Forbidden
from .logging_ import info
from .notify import SERVICE_FA
from .policies import Actor

RESOURCE = "ai_summary"


def _offline_summary(b: dict) -> str:
    svc = SERVICE_FA.get(b["service"], b["service"])
    note = (b["note"] or "").strip()
    head = f"درخواست {svc} — وضعیت: {b['status']}."
    return head if not note else f"{head} یادداشت بیمار: {note[:220]}"


def summarize_booking(actor: Actor, booking: dict) -> dict[str, object]:
    if not actor.at_least("staff"):
        raise Forbidden()

    # layer 09 — per user, global, and the shared breaker
    ratelimit.breaker_check()
    ratelimit.enforce(
        ratelimit.id_bucket("ai", f"user:{actor.user_id}"),
        settings.RL_AI_USER_PER_HOUR,
        3600,
        message="سقف استفاده‌ی ساعتی شما از این ابزار پر شده است.",
    )
    ratelimit.enforce(
        "ai:global",
        settings.RL_AI_GLOBAL_PER_DAY,
        86400,
        message="سقف روزانه‌ی این ابزار برای کل سامانه پر شده است.",
    )

    if not settings.AI_ENABLED or not settings.AI_API_KEY:
        return {"summary": _offline_summary(booking), "mode": "offline", "cost_usd": 0.0}

    # layer 06 — check the cap before spending a cent
    budget.guard(RESOURCE, 1.0)
    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.AI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "max_tokens": 160,
                "messages": [
                    {
                        "role": "system",
                        "content": "خلاصه‌ی یک‌خطی و خنثی از درخواست نوبت بنویس. "
                        "هیچ توصیه‌ی پزشکی نده.",
                    },
                    {"role": "user", "content": _offline_summary(booking)},
                ],
            },
            timeout=12.0,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        tracking.notify("ai_call_failed", "AI provider call failed", exc_type=type(exc).__name__)
        raise DependencyFailed("سرویس خلاصه‌سازی در دسترس نیست.") from exc

    cost = budget.record(RESOURCE, 1.0)
    info("ai.call_ok", resource=RESOURCE, cost_usd=round(cost, 6))
    return {"summary": text, "mode": "live", "cost_usd": round(cost, 6)}
