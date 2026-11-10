"""Reviewer-defined normalized terms for categorical fields.

Original terms are stored on the findings exactly as written and are never
rewritten. A reviewer may record that an original term should be *grouped
under* a normalized term (for example two spellings of one outcome). The
mapping is exact (one original term of one field -> one normalized term), is
recorded with who made it and when, can be withdrawn, and is applied only when
tables are grouped - always together with the original terms. Terms without a
mapping are never merged with anything, however similar they look.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import audit
from .db import transaction
from .errors import NotFoundError, ReviewError, ValidationError
from .util import Actor, utc_now

FIELDS = ("system_context", "experimental_context", "intervention", "comparator", "outcome", "measurement_method")


def _field(field: str) -> str:
    if field not in FIELDS:
        raise ValidationError(f"field must be one of {list(FIELDS)}, got {field!r}")
    return field


def set_term(conn: sqlite3.Connection, field: str, original_term: str, normalized_term: str, actor: Actor,
             note: str | None = None) -> dict[str, Any]:
    """Group ``original_term`` of ``field`` under ``normalized_term`` (a named reviewer's decision)."""
    _field(field)
    if actor.kind not in ("human", "simulated"):
        raise ReviewError("A term mapping needs a named reviewer, not an automated component.")
    if actor.kind == "simulated" and \
            conn.execute("SELECT data_class FROM project").fetchone()["data_class"] != "synthetic":
        raise ReviewError("Simulated review actions are only permitted in synthetic projects.")
    if not original_term or not original_term.strip() or not normalized_term or not normalized_term.strip():
        raise ValidationError("Both the original term and the normalized term are required.")
    normalized_term = normalized_term.strip()
    used = conn.execute(f"SELECT COUNT(*) FROM finding WHERE {field} = ?", (original_term,)).fetchone()[0]  # noqa: S608
    if not used:
        raise ValidationError(f"No finding has {field} = {original_term!r}; a mapping must name a term exactly "
                              "as it is stored.")
    row = conn.execute("SELECT * FROM vocabulary_term WHERE field = ? AND original_term = ?",
                       (field, original_term)).fetchone()
    now = utc_now()
    with transaction(conn):
        if row:
            conn.execute("UPDATE vocabulary_term SET normalized_term = ?, note = ?, active = 1, created_at = ?,"
                         " created_by = ? WHERE id = ?", (normalized_term, note, now, actor.name, row["id"]))
        else:
            conn.execute("INSERT INTO vocabulary_term (field, original_term, normalized_term, note, created_at,"
                         " created_by) VALUES (?,?,?,?,?,?)",
                         (field, original_term, normalized_term, note, now, actor.name))
        audit.record(conn, actor, "vocabulary.set", "vocabulary_term", f"{field}:{original_term}", {
            "field": field, "original_term": original_term, "normalized_term": normalized_term, "note": note,
            "before": ({"normalized_term": row["normalized_term"], "active": bool(row["active"])} if row else None),
            "findings_with_term": used})
    return {"field": field, "original_term": original_term, "normalized_term": normalized_term,
            "findings_with_term": used}


def remove_term(conn: sqlite3.Connection, field: str, original_term: str, actor: Actor, reason: str) -> None:
    """Withdraw a mapping (the row is kept, inactive)."""
    _field(field)
    if not reason or not reason.strip():
        raise ValidationError("A reason is required.")
    row = conn.execute("SELECT * FROM vocabulary_term WHERE field = ? AND original_term = ? AND active = 1",
                       (field, original_term)).fetchone()
    if row is None:
        raise NotFoundError(f"No active mapping for {field} = {original_term!r}")
    with transaction(conn):
        conn.execute("UPDATE vocabulary_term SET active = 0 WHERE id = ?", (row["id"],))
        audit.record(conn, actor, "vocabulary.remove", "vocabulary_term", f"{field}:{original_term}",
                     {"field": field, "original_term": original_term, "normalized_term": row["normalized_term"],
                      "reason": reason})


def mapping(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """Active mappings as ``{field: {original_term: normalized_term}}``."""
    out: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT field, original_term, normalized_term FROM vocabulary_term WHERE active = 1"):
        out.setdefault(r["field"], {})[r["original_term"]] = r["normalized_term"]
    return out


def terms(conn: sqlite3.Connection, field: str | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    sql, params = "SELECT * FROM vocabulary_term WHERE 1 = 1", []
    if field:
        sql += " AND field = ?"
        params.append(_field(field))
    if not include_inactive:
        sql += " AND active = 1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY field, normalized_term, original_term", params)]


def usage(conn: sqlite3.Connection, field: str, limit: int | None = 500) -> list[dict[str, Any]]:
    """Original terms of ``field`` in active findings with counts and the normalized term each is grouped under."""
    _field(field)
    mapped = mapping(conn).get(field, {})
    sql = (f"SELECT {field} AS term, COUNT(*) AS n FROM finding WHERE record_status = 'active' AND {field} IS NOT NULL"  # noqa: S608
           f" GROUP BY {field} ORDER BY n DESC, term")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [{"original_term": r["term"], "findings": r["n"], "normalized_term": mapped.get(r["term"])}
            for r in conn.execute(sql)]


def group_label(mapped: dict[str, str], original_term: str | None) -> tuple[str | None, bool]:
    """``(label used for grouping, True when it comes from a reviewer mapping)``."""
    if original_term is None or original_term == "":
        return None, False
    if original_term in mapped:
        return mapped[original_term], True
    return original_term, False
