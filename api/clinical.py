"""The clinical record: extensible catalogues and the patient's treatment history.

Two catalogues (`body_parts`, `treatments`) start with a seeded list and grow: when a
clinician types something that is not there, it is added once and is then available to
everyone from the dropdown.
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from . import repo
from .errors import BadRequest, Conflict
from .jalali import now_tehran, to_jalali_long, to_jalali_str
from .logging_ import info
from .policies import SYSTEM, Actor
from .schemas import clean_text

CATEGORY_FA = {
    "spine": "ستون فقرات",
    "upper": "اندام فوقانی",
    "lower": "اندام تحتانی",
    "neuro": "عصبی",
    "other": "سایر",
}
CATEGORY_ORDER = ["spine", "upper", "lower", "neuro", "other"]

# A patient coming back after this long gets a thank-you message.
RETURN_GAP_DAYS = 30


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# catalogues
# --------------------------------------------------------------------------
def catalog(actor: Actor) -> dict[str, Any]:
    parts = repo.select(actor, "body_parts", where="is_active = 1", order_by="name")
    treats = repo.select(actor, "treatments", where="is_active = 1", order_by="name")
    grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORY_ORDER}
    for p in parts:
        grouped.setdefault(p["category"], []).append(
            {"id": p["id"], "name": p["name"], "builtin": bool(p["is_builtin"])}
        )
    return {
        "body_parts": [
            {"category": c, "label": CATEGORY_FA[c], "items": grouped.get(c, [])}
            for c in CATEGORY_ORDER
            if grouped.get(c)
        ],
        "treatments": [
            {"id": t["id"], "name": t["name"], "builtin": bool(t["is_builtin"])} for t in treats
        ],
    }


def add_catalog_item(
    actor: Actor, table: str, name: str, category: str = "other"
) -> dict[str, Any]:
    """Add a missing entry. Typing it once is enough — it stays in the list."""
    if table not in ("body_parts", "treatments"):
        raise BadRequest("فهرست نامعتبر است.")
    clean = clean_text(name, max_len=60)
    if len(clean) < 2:
        raise BadRequest("عنوان باید حداقل ۲ نویسه باشد.", fields={"name": "خیلی کوتاه"})

    existing = repo.select(
        actor, table, where="lower(trim(name)) = lower(trim(?))", params=[clean], limit=1
    )
    if existing:
        row = existing[0]
        if not row["is_active"]:
            repo.update(actor, table, {"is_active": 1}, where="id = ?", params=[row["id"]])
            return {"id": row["id"], "name": row["name"], "reactivated": True}
        raise Conflict("این مورد از قبل در فهرست هست.", code="catalog_duplicate")

    data: dict[str, Any] = {"name": clean, "is_builtin": 0, "created_by": actor.user_id}
    if table == "body_parts":
        if category not in CATEGORY_FA:
            category = "other"
        data["category"] = category
    new_id = repo.insert(actor, table, data)
    info("catalog.added", table=table, item_id=new_id)
    return {"id": new_id, "name": clean, "category": data.get("category"), "reactivated": False}


# --------------------------------------------------------------------------
# treatment history
# --------------------------------------------------------------------------
def _labels(actor: Actor, table: str, ids: list[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = repo.select(actor, table, where=f"id IN ({marks})", params=ids, order_by="name")
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def record_session(
    actor: Actor,
    patient_id: int,
    *,
    body_part_ids: list[int],
    treatment_ids: list[int],
    findings: str = "",
    plan: str = "",
    pain_before: int | None = None,
    pain_after: int | None = None,
    appointment_id: int | None = None,
    session_date: str | None = None,
) -> dict[str, Any]:
    if not treatment_ids:
        raise BadRequest(
            "حداقل یک درمان انجام‌شده را انتخاب کنید.", fields={"treatment_ids": "خالی است"}
        )
    if not body_part_ids:
        raise BadRequest(
            "حداقل یک عضو یا آسیب درگیر را انتخاب کنید.", fields={"body_part_ids": "خالی است"}
        )

    valid_parts = {r["id"] for r in _labels(actor, "body_parts", body_part_ids)}
    valid_treats = {r["id"] for r in _labels(actor, "treatments", treatment_ids)}
    if set(body_part_ids) - valid_parts:
        raise BadRequest("عضو یا آسیب انتخاب‌شده در فهرست نیست.")
    if set(treatment_ids) - valid_treats:
        raise BadRequest("درمان انتخاب‌شده در فهرست نیست.")

    day = session_date or now_tehran().date().isoformat()
    public_id = secrets.token_hex(5)
    with repo.tx() as conn:
        cur = conn.execute(
            "INSERT INTO treatment_sessions(public_id, patient_id, appointment_id,"
            " session_date, findings, plan, pain_before, pain_after, created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                public_id,
                patient_id,
                appointment_id,
                day,
                clean_text(findings, max_len=2000),
                clean_text(plan, max_len=2000),
                pain_before,
                pain_after,
                actor.user_id,
            ),
        )
        sid = int(cur.lastrowid or 0)
        conn.executemany(
            "INSERT INTO session_body_parts(session_id, body_part_id) VALUES (?,?)",
            [(sid, b) for b in sorted(set(body_part_ids))],
        )
        conn.executemany(
            "INSERT INTO session_treatments(session_id, treatment_id) VALUES (?,?)",
            [(sid, t) for t in sorted(set(treatment_ids))],
        )
    info(
        "clinical.session_recorded",
        session_id=sid,
        patient_id=patient_id,
        parts=len(set(body_part_ids)),
        treatments=len(set(treatment_ids)),
    )
    return {"id": sid, "public_id": public_id, "session_date": day}


def history(actor: Actor, patient_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = repo.select(
        actor,
        "treatment_sessions",
        where="patient_id = ?",
        params=[patient_id],
        order_by="session_date desc",
        limit=limit,
    )
    rows.sort(key=lambda r: (r["session_date"], r["id"]), reverse=True)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ",".join("?" for _ in ids)

    parts_by: dict[int, list[int]] = {}
    for r in repo.select(actor, "session_body_parts", where=f"session_id IN ({marks})", params=ids):
        parts_by.setdefault(r["session_id"], []).append(r["body_part_id"])
    treats_by: dict[int, list[int]] = {}
    for r in repo.select(actor, "session_treatments", where=f"session_id IN ({marks})", params=ids):
        treats_by.setdefault(r["session_id"], []).append(r["treatment_id"])

    all_parts = {
        x["id"]: x["name"]
        for x in _labels(actor, "body_parts", sorted({i for v in parts_by.values() for i in v}))
    }
    all_treats = {
        x["id"]: x["name"]
        for x in _labels(actor, "treatments", sorted({i for v in treats_by.values() for i in v}))
    }

    out = []
    for i, r in enumerate(rows):
        prev = rows[i + 1]["session_date"] if i + 1 < len(rows) else None
        gap = None
        if prev:
            gap = (dt.date.fromisoformat(r["session_date"]) - dt.date.fromisoformat(prev)).days
        out.append(
            {
                "public_id": r["public_id"],
                "session_date": r["session_date"],
                "jalali": to_jalali_str(r["session_date"]),
                "label": to_jalali_long(r["session_date"]),
                "body_parts": [all_parts[i] for i in parts_by.get(r["id"], []) if i in all_parts],
                "treatments": [
                    all_treats[i] for i in treats_by.get(r["id"], []) if i in all_treats
                ],
                "findings": r["findings"],
                "plan": r["plan"],
                "pain_before": r["pain_before"],
                "pain_after": r["pain_after"],
                "gap_days": gap,
                "created_at": r["created_at"],
            }
        )
    return out


def summary(actor: Actor, patient_id: int) -> dict[str, Any]:
    h = history(actor, patient_id)
    if not h:
        return {"sessions": 0, "first": None, "last": None, "body_parts": [], "treatments": []}
    parts: list[str] = []
    treats: list[str] = []
    for s in h:
        for p in s["body_parts"]:
            if p not in parts:
                parts.append(p)
        for t in s["treatments"]:
            if t not in treats:
                treats.append(t)
    return {
        "sessions": len(h),
        "first": h[-1]["label"],
        "last": h[0]["label"],
        "body_parts": parts,
        "treatments": treats,
    }


# --------------------------------------------------------------------------
# "welcome back" rule
# --------------------------------------------------------------------------
def last_visit(patient_id: int, before: str | None = None) -> str | None:
    """The most recent day this patient was actually seen or booked."""
    where = "patient_id = ? AND status != 'cancelled'"
    params: list[Any] = [patient_id]
    if before:
        where += " AND slot_date < ?"
        params.append(before)
    appts = repo.select(SYSTEM, "appointments", columns=["slot_date"], where=where, params=params)
    days = [a["slot_date"] for a in appts]
    s_where = "patient_id = ?"
    s_params: list[Any] = [patient_id]
    if before:
        s_where += " AND session_date < ?"
        s_params.append(before)
    sess = repo.select(
        SYSTEM, "treatment_sessions", columns=["session_date"], where=s_where, params=s_params
    )
    days += [s["session_date"] for s in sess]
    return max(days) if days else None


def returning_after_gap(
    patient_id: int, new_date: str, gap_days: int = RETURN_GAP_DAYS
) -> int | None:
    """Days since the previous visit, if the gap is long enough to thank them."""
    prev = last_visit(patient_id, before=new_date)
    if not prev:
        return None
    gap = (dt.date.fromisoformat(new_date) - dt.date.fromisoformat(prev)).days
    return gap if gap >= gap_days else None
