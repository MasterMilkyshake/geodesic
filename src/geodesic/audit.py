"""Append-only, hash-chained audit log.

Every state change made through the application records an event. The table
rejects UPDATE and DELETE (triggers), and each row stores the hash of the
previous row, so truncation or in-place edits made outside the application
are detectable with :func:`verify_chain`.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Iterable

from .util import Actor, canonical_json, utc_now

GENESIS_HASH = "0" * 64


def _last_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row else GENESIS_HASH


def _event_hash(prev_hash: str, ts: str, actor: str, actor_kind: str, action: str,
                entity_type: str, entity_id: str | None, details_json: str) -> str:
    payload = "\x1f".join([prev_hash, ts, actor, actor_kind, action, entity_type, entity_id or "", details_json])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(conn: sqlite3.Connection, actor: Actor, action: str, entity_type: str,
           entity_id: str | None, details: dict[str, Any] | None = None) -> int:
    """Append one audit event and return its sequence number."""
    ts = utc_now()
    details_json = canonical_json(details or {})
    prev = _last_hash(conn)
    h = _event_hash(prev, ts, actor.name, actor.kind, action, entity_type, entity_id, details_json)
    cur = conn.execute(
        "INSERT INTO audit_event (ts, actor, actor_kind, action, entity_type, entity_id, details_json, prev_hash, hash)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, actor.name, actor.kind, action, entity_type, entity_id, details_json, prev, h),
    )
    return int(cur.lastrowid)


def record_many(conn: sqlite3.Connection, actor: Actor,
                events: Iterable[tuple[str, str, str | None, dict[str, Any] | None]]) -> int:
    """Append many events ``(action, entity_type, entity_id, details)`` efficiently."""
    ts = utc_now()
    prev = _last_hash(conn)
    rows = []
    for action, entity_type, entity_id, details in events:
        details_json = canonical_json(details or {})
        h = _event_hash(prev, ts, actor.name, actor.kind, action, entity_type, entity_id, details_json)
        rows.append((ts, actor.name, actor.kind, action, entity_type, entity_id, details_json, prev, h))
        prev = h
    if rows:
        conn.executemany(
            "INSERT INTO audit_event (ts, actor, actor_kind, action, entity_type, entity_id, details_json, prev_hash, hash)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def verify_chain(conn: sqlite3.Connection) -> tuple[bool, int, str | None]:
    """Recompute the hash chain. Returns ``(ok, events_checked, first_problem)``."""
    prev = GENESIS_HASH
    n = 0
    for row in conn.execute(
        "SELECT seq, ts, actor, actor_kind, action, entity_type, entity_id, details_json, prev_hash, hash"
        " FROM audit_event ORDER BY seq"
    ):
        n += 1
        if row["prev_hash"] != prev:
            return False, n, f"event {row['seq']}: previous-hash link is broken"
        expect = _event_hash(prev, row["ts"], row["actor"], row["actor_kind"], row["action"],
                             row["entity_type"], row["entity_id"], row["details_json"])
        if expect != row["hash"]:
            return False, n, f"event {row['seq']}: content does not match its hash"
        prev = row["hash"]
    return True, n, None


def history(conn: sqlite3.Connection, entity_type: str | None = None, entity_id: str | None = None,
            action_prefix: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    """Audit events, newest first, optionally filtered by entity or action prefix."""
    where, params = [], []
    if entity_type:
        where.append("entity_type = ?")
        params.append(entity_type)
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    if action_prefix:
        where.append("action LIKE ?")
        params.append(action_prefix + "%")
    sql = "SELECT * FROM audit_event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY seq DESC LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [dict(r) for r in conn.execute(sql, params)]


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0])
