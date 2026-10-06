"""Layer 04 — authorization lives here, in the data layer, not in the UI.

Every table in the schema must appear in POLICIES. `repo` refuses to touch a table that
has no entry, so adding a table without thinking about access is a hard failure, not a
silent hole. SQLite has no row level security, so the predicate is a SQL fragment that
`repo` *always* appends to the statement it builds — there is no code path that reaches a
table without passing through here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

Action = Literal["select", "insert", "update", "delete"]
Role = Literal["anon", "staff", "owner", "system"]

# role inheritance: owner can do everything staff can
ROLE_RANK: dict[str, int] = {"anon": 0, "staff": 10, "owner": 20, "system": 99}


@dataclass(frozen=True)
class Actor:
    role: Role = "anon"
    user_id: int | None = None
    session_id: str | None = None

    def at_least(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


ANON = Actor("anon")
SYSTEM = Actor("system")


@dataclass(frozen=True)
class TablePolicy:
    """Minimum role per action, plus an optional row predicate."""

    select: Role | None = None
    insert: Role | None = None
    update: Role | None = None
    delete: Role | None = None
    # predicate(actor) -> (sql_fragment, params) appended with AND to every statement
    row_filter: Callable[[Actor], tuple[str, list[Any]]] | None = None
    # columns a non-owner actor may never read
    hidden_columns: frozenset[str] = field(default_factory=frozenset)
    note: str = ""


def _own_sessions(actor: Actor) -> tuple[str, list[Any]]:
    if actor.role == "system" or actor.at_least("owner"):
        return ("1=1", [])
    return ("user_id = ?", [actor.user_id if actor.user_id is not None else -1])


POLICIES: dict[str, TablePolicy] = {
    # ---- identity -------------------------------------------------------
    "admin_users": TablePolicy(
        select="owner",
        insert="system",
        update="system",
        delete="owner",
        hidden_columns=frozenset({"password_hash"}),
        note="Only the owner lists staff. Hashes are never selectable through repo.",
    ),
    "sessions": TablePolicy(
        select="staff",
        insert="system",
        update="system",
        delete="staff",
        row_filter=_own_sessions,
        note="Staff may only see and revoke their own sessions.",
    ),
    # ---- the product ----------------------------------------------------
    "bookings": TablePolicy(
        select="staff",
        insert="system",
        update="staff",
        delete="owner",
        note="Anonymous visitors create bookings through the service layer "
        "(system actor) after validation + rate limiting; they can never read any.",
    ),
    "deliveries": TablePolicy(
        select="staff",
        insert="system",
        update="system",
        delete="owner",
        note="Delivery receipts are written by the worker only.",
    ),
    # ---- patient portal --------------------------------------------------
    "patients": TablePolicy(
        select="staff",
        insert="system",
        update="staff",
        delete="owner",
        note="Patients never read the table directly; the portal resolves exactly one "
        "row through the system actor after a session check. Staff see all.",
    ),
    "patient_sessions": TablePolicy(
        select="system",
        insert="system",
        update="system",
        delete="system",
        note="Session plumbing. Not reachable from any HTTP role.",
    ),
    "appointments": TablePolicy(
        select="staff",
        insert="system",
        update="staff",
        delete="owner",
        note="Staff may reschedule; only the owner may delete. Patients reach their own "
        "rows through the service layer, scoped by patient_id.",
    ),
    "sms_messages": TablePolicy(
        select="staff",
        insert="system",
        update="system",
        delete="owner",
        note="Message log, including what was sent to whom.",
    ),
    "sms_inbound": TablePolicy(select="staff", insert="system", update="system", delete="owner"),
    "messages": TablePolicy(
        select="staff",
        insert="system",
        update="system",
        delete="owner",
        note="The doctor<->patient thread. Patients only ever reach their own rows "
        "through the service layer (system actor, scoped by patient_id); staff see all. "
        "Read markers are stamped by the service layer, not by either HTTP role.",
    ),
    # ---- clinical record ---------------------------------------------------
    "body_parts": TablePolicy(
        select="staff",
        insert="staff",
        update="staff",
        delete="owner",
        note="Catalogue of affected areas and diagnoses. Staff may add a missing "
        "entry; only the owner may remove one.",
    ),
    "treatments": TablePolicy(
        select="staff",
        insert="staff",
        update="staff",
        delete="owner",
        note="Catalogue of treatments the clinic performs. Extensible by staff.",
    ),
    "treatment_sessions": TablePolicy(
        select="staff",
        insert="staff",
        update="staff",
        delete="owner",
        note="The patient's treatment history — clinical staff data; the patient "
        "portal never reads it.",
    ),
    "session_body_parts": TablePolicy(
        select="staff", insert="staff", update="staff", delete="staff"
    ),
    "session_treatments": TablePolicy(
        select="staff", insert="staff", update="staff", delete="staff"
    ),
    # ---- infrastructure --------------------------------------------------
    "idempotency_keys": TablePolicy(
        select="system",
        insert="system",
        update="system",
        delete="system",
        note="Request plumbing. Never reachable from any HTTP role.",
    ),
    "rate_events": TablePolicy(select="system", insert="system", update="system", delete="system"),
    "breaker_state": TablePolicy(
        select="system", insert="system", update="system", delete="system"
    ),
    "cost_events": TablePolicy(
        select="owner",
        insert="system",
        update="system",
        delete="system",
        note="Spend is owner-only.",
    ),
    "outcome_events": TablePolicy(
        select="staff",
        insert="system",
        update="system",
        delete="owner",
        note="Layer 14 metric.",
    ),
    "schema_migrations": TablePolicy(
        select="owner", insert="system", update="system", delete="system"
    ),
}


def policy_for(table: str) -> TablePolicy | None:
    return POLICIES.get(table)


def missing_policies(all_tables: list[str]) -> list[str]:
    return [t for t in all_tables if t not in POLICIES]
