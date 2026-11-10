"""SQLite access: connections, transactions, migrations with pre-migration backups.

SQLAlchemy is deliberately not used (see DECISIONS.md): the schema is fixed,
the application is single-user and local, and plain SQL keeps every query
visible and the dependency set small.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Iterator, Sequence

from ..errors import GeodesicError
from . import schema_v1

log = logging.getLogger("geodesic.db")

#: Ordered list of migration modules. Each exposes VERSION, DESCRIPTION and upgrade(conn).
MIGRATIONS: list[ModuleType] = [schema_v1]


def latest_version(migrations: Sequence[ModuleType] | None = None) -> int:
    return max(m.VERSION for m in (migrations or MIGRATIONS))


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with foreign keys on, WAL journaling and explicit transactions."""
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def refresh_statistics(conn: sqlite3.Connection) -> None:
    """Bring the query planner's table statistics up to date (cheap when nothing changed much).

    Without statistics SQLite cannot tell a selective index (``paper_id``) from
    an unselective one (``record_status``) and may pick a plan that is quadratic
    at 100,000 findings. ``PRAGMA optimize`` with mask ``0x10002`` re-analyses
    only tables that were never analysed or whose size changed ten-fold, under
    an analysis limit. It is called when a project is opened and closed and
    after each bulk import. Statistics are not evidence and are not audited.
    """
    try:
        conn.execute("PRAGMA optimize = 0x10002")
    except sqlite3.Error:                                   # e.g. a read-only database file
        pass


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic block. Nested use creates savepoints, so helpers can be composed safely."""
    if conn.in_transaction:
        # SQLite resolves a savepoint name to the most recent one, so a constant name nests correctly.
        conn.execute("SAVEPOINT geodesic_sp")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK TO geodesic_sp")
            conn.execute("RELEASE geodesic_sp")
            raise
        else:
            conn.execute("RELEASE geodesic_sp")
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements (trigger bodies stay intact)."""
    statements: list[str] = []
    buf = ""
    for line in script.splitlines():
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("--")):
            continue
        buf += line + "\n"
        if sqlite3.complete_statement(buf):
            statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise GeodesicError(f"Incomplete SQL statement in migration script: {buf[:80]!r}")
    return statements


def backup_database(conn: sqlite3.Connection, backup_dir: Path, label: str) -> Path:
    """Write a consistent copy of the database using SQLite's online backup API."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = backup_dir / f"geodesic-{label}-{stamp}.sqlite"
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return target


def migrate(
    conn: sqlite3.Connection,
    backup_dir: Path,
    migrations: Sequence[ModuleType] | None = None,
) -> list[int]:
    """Apply pending migrations in order and return the versions applied.

    When an existing database (version > 0) needs upgrading, a backup is written
    to ``backup_dir`` *before* the first migration runs. Each migration runs in
    its own transaction; a failure rolls that migration back and re-raises.
    """
    migs = sorted(migrations or MIGRATIONS, key=lambda m: m.VERSION)
    current = schema_version(conn)
    target = max(m.VERSION for m in migs)
    if current > target:
        raise GeodesicError(
            f"Database schema version {current} is newer than this application supports ({target}). "
            "Upgrade GEODESIC, or restore a backup made by the older version."
        )
    pending = [m for m in migs if m.VERSION > current]
    if not pending:
        return []
    if current > 0:
        path = backup_database(conn, backup_dir, f"pre-migration-v{current}")
        log.info("pre-migration backup written", extra={"backup_path": str(path), "from_version": current})
    applied: list[int] = []
    for mig in pending:
        conn.execute("BEGIN IMMEDIATE")
        try:
            mig.upgrade(_StatementRunner(conn))
            conn.execute(f"PRAGMA user_version = {int(mig.VERSION)}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        applied.append(mig.VERSION)
        log.info("migration applied", extra={"version": mig.VERSION, "description": mig.DESCRIPTION})
    return applied


class _StatementRunner:
    """Connection facade given to migrations so scripts run inside the open transaction.

    ``sqlite3.Connection.executescript`` would commit the surrounding transaction;
    this splits the script and executes statements one by one instead.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def executescript(self, script: str) -> None:
        for stmt in split_statements(script):
            self._conn.execute(stmt)

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Sequence[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]
