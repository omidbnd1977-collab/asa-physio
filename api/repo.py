"""Layer 03 + 04 — the only way application code reaches the database.

Every statement is parameterised, every table is checked against `policies.POLICIES`
before a single byte of SQL is built, and the policy row filter is always appended.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from . import policies as P
from .db import get_conn
from .errors import Forbidden, PolicyMissing
from .policies import Actor, TablePolicy

IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not IDENT.match(name):
        raise ValueError(f"illegal identifier: {name!r}")
    return name


def _policy(table: str) -> TablePolicy:
    pol = P.policy_for(_ident(table))
    if pol is None:
        # fail closed: an unlisted table is unreachable, not open
        raise PolicyMissing(detail=f"no policy declared for table {table!r}")
    return pol


def _authorize(actor: Actor, table: str, action: str) -> TablePolicy:
    pol = _policy(table)
    required = getattr(pol, action)
    if required is None:
        raise Forbidden(detail=f"{action} not allowed on {table}")
    if not actor.at_least(required):
        raise Forbidden(detail=f"{actor.role} < {required} for {action} on {table}")
    return pol


def _row_filter(pol: TablePolicy, actor: Actor) -> tuple[str, list[Any]]:
    if pol.row_filter is None:
        return "1=1", []
    return pol.row_filter(actor)


def _visible(pol: TablePolicy, actor: Actor, columns: Sequence[str] | None) -> list[str] | None:
    if not pol.hidden_columns or actor.role == "system":
        return list(columns) if columns else None
    if columns:
        bad = set(columns) & pol.hidden_columns
        if bad:
            raise Forbidden(detail=f"hidden columns requested: {sorted(bad)}")
        return list(columns)
    return None  # filtered after fetch


def select(
    actor: Actor,
    table: str,
    *,
    columns: Sequence[str] | None = None,
    where: str = "1=1",
    params: Iterable[Any] = (),
    order_by: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    pol = _authorize(actor, table, "select")
    cols = _visible(pol, actor, columns)
    col_sql = ", ".join(_ident(c) for c in cols) if cols else "*"
    rf, rp = _row_filter(pol, actor)
    # identifiers pass _ident(); every value is a bound parameter
    sql = f"SELECT {col_sql} FROM {_ident(table)} WHERE ({where}) AND ({rf})"  # noqa: S608
    args = list(params) + rp
    if order_by:
        if not re.match(r"^[a-z_][a-z0-9_]*( (asc|desc))?$", order_by, re.I):
            raise ValueError("illegal order_by")
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
    rows = [dict(r) for r in get_conn().execute(sql, args).fetchall()]
    if pol.hidden_columns and actor.role != "system":
        rows = [{k: v for k, v in r.items() if k not in pol.hidden_columns} for r in rows]
    return rows


def count(actor: Actor, table: str, *, where: str = "1=1", params: Iterable[Any] = ()) -> int:
    pol = _authorize(actor, table, "select")
    rf, rp = _row_filter(pol, actor)
    sql = f"SELECT COUNT(*) AS n FROM {_ident(table)} WHERE ({where}) AND ({rf})"  # noqa: S608
    return int(get_conn().execute(sql, list(params) + rp).fetchone()["n"])


def insert(actor: Actor, table: str, data: dict[str, Any]) -> int:
    _authorize(actor, table, "insert")
    if not data:
        raise ValueError("insert needs at least one column")
    cols = [_ident(c) for c in data]
    placeholders = ", ".join("?" for _ in cols)
    # identifiers pass _ident(); every value is a bound parameter
    sql = f"INSERT INTO {_ident(table)} ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
    cur = get_conn().execute(sql, list(data.values()))
    return int(cur.lastrowid or 0)


def update(
    actor: Actor, table: str, data: dict[str, Any], *, where: str, params: Iterable[Any] = ()
) -> int:
    pol = _authorize(actor, table, "update")
    if not data:
        return 0
    sets = ", ".join(f"{_ident(c)} = ?" for c in data)
    rf, rp = _row_filter(pol, actor)
    sql = f"UPDATE {_ident(table)} SET {sets} WHERE ({where}) AND ({rf})"  # noqa: S608
    cur = get_conn().execute(sql, list(data.values()) + list(params) + rp)
    return cur.rowcount


def delete(actor: Actor, table: str, *, where: str, params: Iterable[Any] = ()) -> int:
    pol = _authorize(actor, table, "delete")
    rf, rp = _row_filter(pol, actor)
    sql = f"DELETE FROM {_ident(table)} WHERE ({where}) AND ({rf})"  # noqa: S608
    return get_conn().execute(sql, list(params) + rp).rowcount


class tx:
    """Explicit transaction. Any exception rolls the whole thing back."""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_conn()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
