"""The single source-location system.

Every piece of evidence points at stored text through one of five location
kinds. All of them resolve against text held in the project database, so a
location can be re-checked at any time without the original file.

==============  =======================================================================
kind            address
==============  =======================================================================
workbook_cell   workbook_version_id + sheet + cell_ref (A1 style) + char span in the
                stored cell text
pdf_page        snapshot_id (document version) + unit_key = page index ("0" = first
                page) + char span in the stored page-text snapshot
xml_element     snapshot_id + unit_key = stable element/section identifier + char span
html_element    snapshot_id + unit_key = stable element identifier + char span
table_cell      snapshot_id + table_key + row_idx + col_idx (0-based) + char span in
                the stored cell text
==============  =======================================================================

Indexing conventions: character spans are 0-based, half-open ``[start, end)``
offsets counted in Unicode code points of the stored text. PDF page indices and
table rows/columns are 0-based. Workbook rows/columns follow the spreadsheet
(``A1``). A span of ``None`` addresses the whole stored text of the unit.

Screening decisions made from a bibliographic record (title/abstract stage) use
one further address, ``record_field``: paper record + field (``title`` or
``abstract``) + char span in the stored field text. It is stored with the
decision and verified by :mod:`geodesic.screening` under the same span rules;
full-text screening decisions use the document kinds above.

A location *resolves* when the stored text at the address equals the excerpt
recorded with the location. Resolving shows that the excerpt is really there;
it does not show that the evidence was interpreted correctly.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .util import new_id, utc_now

LOCATION_KINDS = ("workbook_cell", "pdf_page", "xml_element", "html_element", "table_cell")
DOCUMENT_KINDS = ("pdf_page", "xml_element", "html_element", "table_cell")
EXCERPT_MAX_CHARS = 600

_COLUMNS = ("id", "owner_type", "owner_id", "owner_version_no", "role", "kind", "workbook_version_id", "sheet",
            "cell_ref", "snapshot_id", "unit_key", "table_key", "row_idx", "col_idx", "char_start", "char_end",
            "excerpt", "resolves", "resolution_note", "is_current", "created_at")
INSERT_SQL = (f"INSERT INTO source_location ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' * len(_COLUMNS))})")


def unit_text(conn: sqlite3.Connection, loc: dict[str, Any]) -> tuple[str | None, str | None]:
    """Stored text of the unit a location addresses: ``(text, problem)``."""
    kind = loc.get("kind")
    if kind == "workbook_cell":
        row = conn.execute(
            "SELECT raw_text FROM source_cell WHERE workbook_version_id = ? AND sheet = ? AND cell_ref = ?",
            (loc.get("workbook_version_id"), loc.get("sheet"), loc.get("cell_ref"))).fetchone()
        if row is None:
            return None, "no stored cell at this workbook version, sheet and cell reference"
        return row["raw_text"] or "", None
    if kind in ("pdf_page", "xml_element", "html_element"):
        row = conn.execute(
            "SELECT text, unit_kind, extraction_status FROM text_unit WHERE snapshot_id = ? AND unit_key = ?",
            (loc.get("snapshot_id"), str(loc.get("unit_key")))).fetchone()
        if row is None:
            return None, "no stored text unit with this snapshot and unit key"
        expected = "page" if kind == "pdf_page" else "element"
        if row["unit_kind"] != expected:
            return None, f"unit is a {row['unit_kind']}, not a {expected}"
        if row["extraction_status"] != "ok":
            return None, f"text extraction for this unit has status '{row['extraction_status']}'"
        return row["text"], None
    if kind == "table_cell":
        row = conn.execute(
            "SELECT c.text FROM doc_table_cell c JOIN doc_table t ON t.id = c.table_id"
            " WHERE t.snapshot_id = ? AND t.table_key = ? AND c.row_idx = ? AND c.col_idx = ?",
            (loc.get("snapshot_id"), loc.get("table_key"), loc.get("row_idx"), loc.get("col_idx"))).fetchone()
        if row is None:
            return None, "no stored table cell at this snapshot, table, row and column"
        return row["text"], None
    return None, f"unknown location kind {kind!r}"


def check_span(text: str | None, start: int | None, end: int | None, excerpt: str | None) -> tuple[bool, str | None]:
    """Does ``text[start:end]`` equal ``excerpt``? ``(ok, problem)``. Pure function."""
    if text is None:
        return False, "no stored text"
    if start is None and end is None:
        if excerpt is None or excerpt == text:
            return True, None
        return False, "excerpt differs from the stored text of the whole unit"
    if start is None or end is None or start < 0 or end > len(text) or start >= end:
        return False, f"span [{start}, {end}) is outside the stored text (length {len(text)})"
    if excerpt is None:
        return True, None
    if text[start:end] != excerpt:
        return False, "stored text at the span differs from the recorded excerpt"
    return True, None


def resolve(conn: sqlite3.Connection, loc: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    """Resolve a location against stored text: ``(resolves, text_at_location, problem)``."""
    text, problem = unit_text(conn, loc)
    if text is None:
        return False, None, problem
    ok, problem = check_span(text, loc.get("char_start"), loc.get("char_end"), loc.get("excerpt"))
    if not ok:
        return False, None, problem
    s, e = loc.get("char_start"), loc.get("char_end")
    return True, (text if s is None else text[s:e]), None


def row_tuple(owner_type: str, owner_id: str, owner_version_no: int | None, loc: dict[str, Any], resolves: bool,
              note: str | None = None, now: str | None = None) -> tuple:
    """Parameter tuple for :data:`INSERT_SQL`."""
    return (new_id("loc"), owner_type, owner_id, owner_version_no, loc.get("role") or "supporting_excerpt",
            loc["kind"], loc.get("workbook_version_id"), loc.get("sheet"), loc.get("cell_ref"),
            loc.get("snapshot_id"), None if loc.get("unit_key") is None else str(loc.get("unit_key")),
            loc.get("table_key"), loc.get("row_idx"), loc.get("col_idx"), loc.get("char_start"),
            loc.get("char_end"), loc.get("excerpt"), 1 if resolves else 0, note, 1, now or utc_now())


def add_location(conn: sqlite3.Connection, owner_type: str, owner_id: str, owner_version_no: int | None,
                 loc: dict[str, Any]) -> tuple[bool, str | None]:
    """Resolve and store one location. Returns ``(resolves, problem)``."""
    ok, _, problem = resolve(conn, loc)
    conn.execute(INSERT_SQL, row_tuple(owner_type, owner_id, owner_version_no, loc, ok, problem))
    return ok, problem


def current_locations(conn: sqlite3.Connection, owner_type: str, owner_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM source_location WHERE owner_type = ? AND owner_id = ? AND is_current = 1"
        " ORDER BY CASE role WHEN 'supporting_excerpt' THEN 0 ELSE 1 END, role, created_at",
        (owner_type, owner_id))]


def describe(loc: dict[str, Any]) -> str:
    """Human-readable address used in tables, reports and exports."""
    span = ""
    if loc.get("char_start") is not None:
        span = f" chars {loc['char_start']}-{loc['char_end']}"
    kind = loc.get("kind")
    if kind == "workbook_cell":
        return f"workbook {loc.get('workbook_version_id')} sheet '{loc.get('sheet')}' cell {loc.get('cell_ref')}{span}"
    if kind == "pdf_page":
        return f"document snapshot {loc.get('snapshot_id')} page index {loc.get('unit_key')}{span}"
    if kind in ("xml_element", "html_element"):
        return f"document snapshot {loc.get('snapshot_id')} element '{loc.get('unit_key')}'{span}"
    if kind == "table_cell":
        return (f"document snapshot {loc.get('snapshot_id')} table '{loc.get('table_key')}' row {loc.get('row_idx')} "
                f"column {loc.get('col_idx')}{span}")
    return "unknown location"


def short_excerpt(text: str | None, limit: int = EXCERPT_MAX_CHARS) -> str:
    """Excerpts in exports are short quotations, never whole documents."""
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
