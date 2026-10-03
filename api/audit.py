"""Durable audit events for sensitive patient and appointment mutations."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from . import repo
from .policies import SYSTEM, Actor


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(
    actor: Actor,
    action: str,
    entity: str,
    entity_id: str | int,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> int:
    """Write a bounded JSON snapshot; secrets and file contents never enter the log."""
    safe_before = before or {}
    safe_after = after or {}
    return repo.insert(
        SYSTEM,
        "audit_logs",
        {
            "actor_role": actor.role,
            "actor_id": actor.user_id,
            "action": action[:80],
            "entity": entity[:80],
            "entity_id": str(entity_id)[:80],
            "before_json": json.dumps(safe_before, ensure_ascii=False, default=str)[:20000],
            "after_json": json.dumps(safe_after, ensure_ascii=False, default=str)[:20000],
            "created_at": _now(),
        },
    )


def list_for(entity: str, entity_id: str | int, *, limit: int = 100) -> list[dict[str, Any]]:
    rows = repo.select(
        SYSTEM,
        "audit_logs",
        where="entity = ? AND entity_id = ?",
        params=[entity, str(entity_id)],
        order_by="created_at desc",
        limit=max(1, min(limit, 200)),
    )
    for row in rows:
        for key in ("before_json", "after_json"):
            try:
                row[key[:-5]] = json.loads(row.pop(key))
            except (TypeError, ValueError):
                row[key[:-5]] = {}
    return rows
