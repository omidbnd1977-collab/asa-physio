"""Layer 06 — every paid resource has a unit cost, a monthly cap and an alert before the cap."""

from __future__ import annotations

import datetime as dt

from . import repo, tracking
from .config import settings
from .errors import BudgetExceeded
from .logging_ import info
from .policies import SYSTEM

# resource -> (unit cost in USD, monthly cap in USD, human label)
CATALOG: dict[str, tuple[float, float, str]] = {
    "ai_summary": (settings.AI_UNIT_COST_USD, settings.BUDGET_MONTHLY_USD, "خلاصه‌سازی هوش مصنوعی"),
    "sms": (
        settings.SMS_UNIT_COST_USD,
        settings.SMS_BUDGET_MONTHLY_USD,
        "پیامک اطلاع‌رسانی به بیمار",
    ),
}


def month_key(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.UTC)).strftime("%Y-%m")


def spent(resource: str, month: str | None = None) -> float:
    m = month or month_key()
    rows = repo.select(
        SYSTEM,
        "cost_events",
        columns=["cost_usd"],
        where="resource = ? AND month = ?",
        params=[resource, m],
    )
    return round(sum(r["cost_usd"] for r in rows), 6)


def summary(month: str | None = None) -> dict[str, dict[str, float | str]]:
    m = month or month_key()
    out: dict[str, dict[str, float | str]] = {}
    for res, (unit, cap, label) in CATALOG.items():
        used = spent(res, m)
        out[res] = {
            "label": label,
            "unit_cost_usd": unit,
            "cap_usd": cap,
            "spent_usd": used,
            "remaining_usd": round(max(0.0, cap - used), 6),
            "pct": round(used / cap * 100, 2) if cap else 0.0,
            "alert_at_pct": round(settings.BUDGET_ALERT_AT * 100, 1),
        }
    return out


def guard(resource: str, units: float = 1.0) -> None:
    """Raise *before* the paid call if the cap would be crossed."""
    unit, cap, label = CATALOG[resource]
    projected = spent(resource) + unit * units
    if projected > cap:
        tracking.notify(
            "budget_cap_reached",
            f"monthly cap reached for {resource}",
            cap_usd=cap,
            projected_usd=projected,
        )
        raise BudgetExceeded(f"سقف هزینه‌ی ماهانه‌ی «{label}» پر شده است.")


def record(resource: str, units: float = 1.0) -> float:
    unit, cap, _ = CATALOG[resource]
    cost = unit * units
    before = spent(resource)
    repo.insert(
        SYSTEM,
        "cost_events",
        {
            "resource": resource,
            "units": units,
            "unit_cost": unit,
            "cost_usd": cost,
            "month": month_key(),
        },
    )
    after = before + cost
    threshold = cap * settings.BUDGET_ALERT_AT
    if before < threshold <= after:
        tracking.notify(
            "budget_alert",
            f"{resource} crossed {int(settings.BUDGET_ALERT_AT * 100)}% of its monthly cap",
            resource=resource,
            spent_usd=round(after, 4),
            cap_usd=cap,
        )
    info("cost.recorded", resource=resource, units=units, cost_usd=round(cost, 6))
    return cost
