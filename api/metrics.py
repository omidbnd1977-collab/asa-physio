"""Layer 14 — the one number, measured from day one.

NORTH STAR: how many booking requests actually reached the clinic.
Everything else on this page is explicitly labelled as supporting or vanity.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import repo
from .policies import SYSTEM

NORTH_STAR = "session_attended"
NORTH_STAR_FA = "جلسه‌ی فیزیوتراپی که واقعاً انجام شد"


def _today() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def record(kind: str, *, booking_id: int | None = None, **meta: Any) -> None:
    repo.insert(
        SYSTEM,
        "outcome_events",
        {
            "kind": kind,
            "booking_id": booking_id,
            "meta": json.dumps(meta, ensure_ascii=False)[:2000],
            "day": _today(),
        },
    )


def _count(kind: str, days: int | None = None) -> int:
    if days is None:
        return repo.count(SYSTEM, "outcome_events", where="kind = ?", params=[kind])
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    return repo.count(SYSTEM, "outcome_events", where="kind = ? AND day >= ?", params=[kind, since])


def outcome(days: int = 30) -> dict[str, Any]:
    registered = _count("patient_registered", days)
    booked = _count("appointment_booked", days) + _count("followup_booked", days)
    attended = _count(NORTH_STAR, days)
    submitted = _count("booking_submitted", days)
    delivered = _count("booking_delivered", days)
    failed = _count("booking_delivery_failed", days)
    return {
        "north_star": {
            "key": NORTH_STAR,
            "label_fa": NORTH_STAR_FA,
            "value": attended,
            "window_days": days,
            "all_time": _count(NORTH_STAR),
        },
        # the funnel that leads to the north star, in order
        "funnel": {
            "patient_registered": registered,
            "appointment_booked": booked,
            "session_attended": attended,
            "book_rate": round(booked / registered * 100, 1) if registered else None,
            "show_rate": round(attended / booked * 100, 1) if booked else None,
        },
        "supporting": {
            "booking_submitted": submitted,
            "booking_delivered": delivered,
            "booking_delivery_failed": failed,
            "delivery_success_rate": round(delivered / submitted * 100, 1) if submitted else None,
            "attendance_confirmed": _count("attendance_confirmed", days),
        },
        "vanity": {  # labelled, so nobody mistakes these for the result
            "call_click": _count("call_click", days),
            "instagram_click": _count("instagram_click", days),
        },
    }


def daily(days: int = 14) -> list[dict[str, Any]]:
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    rows = repo.select(
        SYSTEM, "outcome_events", columns=["day", "kind"], where="day >= ?", params=[since]
    )
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        agg.setdefault(r["day"], {}).setdefault(r["kind"], 0)
        agg[r["day"]][r["kind"]] += 1
    return [{"day": d, **v} for d, v in sorted(agg.items())]
