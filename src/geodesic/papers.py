"""Paper (report) records, identifiers, metadata conflicts and integrity checks.

Separate statuses are kept for separate questions:

* ``resolution_status``: does the record carry a syntactically valid persistent
  identifier (``resolved``) or is it a provisional record made from a citation
  that could not be resolved (``unresolved``)?
* ``identity_status``: has the bibliographic identity been checked
  (``identifier_validated`` by a metadata lookup, ``reviewer_verified`` by a
  person)? Identity verification says nothing about whether findings attributed
  to the paper are correct.
* ``paper_identifier.validation_status``: per identifier.

Incoming metadata never silently replaces existing metadata: empty fields are
filled, differing values are recorded as ``metadata_conflict`` rows.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from . import audit
from .errors import NotFoundError, ValidationError
from .identifiers import normalize_identifier
from .util import Actor, canonical_json, loads, new_id, normalize_text_key, utc_now, utc_today

METADATA_FIELDS = ("title", "authors", "year", "journal", "publication_type", "language", "abstract",
                   "publication_status", "citation_text")
INTEGRITY_RESULTS = ("none_found", "correction", "retraction", "expression_of_concern", "other_notice")
_CHECKED_SCHEMES = ("doi", "pmid", "pmcid")


def first_author_key(authors: Iterable[str] | None) -> str | None:
    """Family-name key of the first author (matching aid only; the author list is stored unchanged)."""
    for a in authors or []:
        a = (a or "").strip()
        if not a:
            continue
        family = a.split(",")[0] if "," in a else a.split()[0] if len(a.split()) > 1 and len(a.split()[-1]) <= 3 \
            and a.split()[-1].isupper() else a.split()[-1]
        return normalize_text_key(family) or None
    return None


def create_paper(conn: sqlite3.Connection, *, origin: str, resolution_status: str, identity_key: str | None = None,
                 workbook_key: str | None = None, citation_text: str | None = None, title: str | None = None,
                 authors: list[str] | None = None, year: int | None = None, journal: str | None = None,
                 publication_type: str | None = None, language: str | None = None, abstract: str | None = None,
                 doi: str | None = None, pmid: str | None = None, pmcid: str | None = None,
                 urls: list[str] | None = None, other_ids: dict | None = None, publication_status: str = "unknown",
                 fulltext_availability: str = "not_checked", paper_id: str | None = None,
                 now: str | None = None) -> str:
    """Insert a paper record and return its stable ID. Identifier columns must already be normalised."""
    pid = paper_id or new_id("pap")
    now = now or utc_now()
    authors = [a for a in (authors or []) if a]
    conn.execute(
        "INSERT INTO paper (id, resolution_status, origin, identity_key, workbook_key, citation_text, title,"
        " title_norm, authors_json, first_author, year, journal, publication_type, language, abstract, doi, pmid,"
        " pmcid, other_ids_json, urls_json, publication_status, fulltext_availability, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, resolution_status, origin, identity_key, workbook_key, citation_text, title,
         normalize_text_key(title) if title else None, canonical_json(authors), first_author_key(authors), year,
         journal, publication_type, language, abstract, doi, pmid, pmcid, canonical_json(other_ids or {}),
         canonical_json(urls or []), publication_status, fulltext_availability, now, now))
    return pid


def add_identifier(conn: sqlite3.Connection, paper_id: str, scheme: str, value_raw: str, source: str,
                   now: str | None = None) -> dict[str, Any]:
    """Record an identifier as supplied. Malformed DOI/PMID/PMCID values are kept and marked ``invalid_syntax``.

    Returns ``{"value_norm", "validation_status", "created"}``.
    """
    raw = str(value_raw).strip()
    if not raw:
        raise ValidationError("An identifier value must not be empty.")
    norm = normalize_identifier(scheme, raw)
    status = "unvalidated"
    if norm is None:
        norm, status = raw.casefold(), "invalid_syntax"
    elif scheme not in _CHECKED_SCHEMES:
        status = "unvalidated"
    existing = conn.execute("SELECT validation_status FROM paper_identifier WHERE paper_id = ? AND scheme = ?"
                            " AND value_norm = ?", (paper_id, scheme, norm)).fetchone()
    if existing:
        return {"value_norm": norm, "validation_status": existing["validation_status"], "created": False}
    conn.execute(
        "INSERT INTO paper_identifier (id, paper_id, scheme, value_raw, value_norm, source, validation_status,"
        " created_at) VALUES (?,?,?,?,?,?,?,?)",
        (new_id("pid"), paper_id, scheme, raw, norm, source, status, now or utc_now()))
    return {"value_norm": norm, "validation_status": status, "created": True}


def record_conflict(conn: sqlite3.Connection, paper_id: str, field: str, existing: Any, incoming: Any,
                    source: str, now: str | None = None) -> bool:
    """Record conflicting metadata (once per distinct incoming value and source)."""
    ex, inc = _as_text(existing), _as_text(incoming)
    dup = conn.execute(
        "SELECT 1 FROM metadata_conflict WHERE paper_id = ? AND field = ? AND incoming_value IS ? AND"
        " incoming_source = ?", (paper_id, field, inc, source)).fetchone()
    if dup:
        return False
    conn.execute(
        "INSERT INTO metadata_conflict (id, paper_id, field, existing_value, incoming_value, incoming_source,"
        " created_at) VALUES (?,?,?,?,?,?,?)", (new_id("mcf"), paper_id, field, ex, inc, source, now or utc_now()))
    return True


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else canonical_json(value)


def _same(field: str, a: Any, b: Any) -> bool:
    if field == "authors":
        return [normalize_text_key(x) for x in a or []] == [normalize_text_key(x) for x in b or []]
    if field == "year":
        return str(a) == str(b)
    return normalize_text_key(str(a)) == normalize_text_key(str(b))


def merge_metadata(conn: sqlite3.Connection, paper_id: str, incoming: dict[str, Any], source: str,
                   now: str | None = None) -> dict[str, list[str]]:
    """Fill empty metadata fields from ``incoming``; record differing values as conflicts.

    Existing values are never overwritten. Returns ``{"filled": [...], "conflicts": [...]}``.
    """
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    now = now or utc_now()
    filled: list[str] = []
    conflicts: list[str] = []
    sets: dict[str, Any] = {}
    for fld in METADATA_FIELDS:
        new = incoming.get(fld)
        if new in (None, "", [], "unknown"):
            continue
        current = loads(row["authors_json"], []) if fld == "authors" else row[fld]
        if current in (None, "", [], "unknown"):
            if fld == "authors":
                sets["authors_json"] = canonical_json(list(new))
                sets["first_author"] = first_author_key(new)
            else:
                sets[fld] = new
                if fld == "title":
                    sets["title_norm"] = normalize_text_key(new)
            filled.append(fld)
        elif not _same(fld, current, new):
            if record_conflict(conn, paper_id, fld, current, new, source, now):
                conflicts.append(fld)
    urls = loads(row["urls_json"], [])
    extra = [u for u in incoming.get("urls") or [] if u and u not in urls]
    if extra:
        sets["urls_json"] = canonical_json(urls + extra)
    if incoming.get("other_ids"):
        other = loads(row["other_ids_json"], {})
        merged = {**incoming["other_ids"], **other}
        if merged != other:
            sets["other_ids_json"] = canonical_json(merged)
    for scheme in _CHECKED_SCHEMES:
        new = incoming.get(scheme)
        if not new:
            continue
        if row[scheme] in (None, ""):
            sets[scheme] = new
            filled.append(scheme)
        elif row[scheme] != new:
            if record_conflict(conn, paper_id, scheme, row[scheme], new, source, now):
                conflicts.append(scheme)
    if sets:
        sets["updated_at"] = now
        conn.execute(f"UPDATE paper SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), paper_id))
    return {"filled": filled, "conflicts": conflicts}


def canonical_id(conn: sqlite3.Connection, paper_id: str) -> str:
    """The canonical record a paper is currently grouped under (itself when ungrouped)."""
    seen = set()
    pid = paper_id
    while pid not in seen:
        seen.add(pid)
        row = conn.execute("SELECT canonical_id FROM paper WHERE id = ?", (pid,)).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown paper {pid}")
        if not row["canonical_id"]:
            return pid
        pid = row["canonical_id"]
    return pid


def set_paper_stages(conn: sqlite3.Connection, paper_id: str, stage_ids: Iterable[str], source: str, actor: Actor,
                     now: str | None = None, status: str = "active") -> int:
    """Add stage links for a paper (existing links are kept; links are never deleted)."""
    now = now or utc_now()
    n = 0
    for sid in stage_ids:
        row = conn.execute("SELECT id, status FROM paper_stage WHERE paper_id = ? AND stage_id = ? AND"
                           " assignment_source = ?", (paper_id, sid, source)).fetchone()
        if row is None:
            conn.execute("INSERT INTO paper_stage (paper_id, stage_id, assignment_source, status, assigned_by,"
                         " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (paper_id, sid, source, status, actor.name, now, now))
            n += 1
        elif row["status"] != status and source != "suggested":
            conn.execute("UPDATE paper_stage SET status = ?, updated_at = ?, assigned_by = ? WHERE id = ?",
                         (status, now, actor.name, row["id"]))
            n += 1
    return n


def record_integrity_check(conn: sqlite3.Connection, paper_id: str, source: str, result: str, actor: Actor,
                           details: str | None = None, notice_url: str | None = None,
                           checked_on: str | None = None) -> str:
    """Record what a named source showed about corrections/retractions on a given date.

    ``none_found`` means exactly that: nothing was found in that source on that
    date. It is never reported as "not retracted".
    """
    if result not in INTEGRITY_RESULTS:
        raise ValidationError(f"result must be one of {INTEGRITY_RESULTS}")
    if conn.execute("SELECT 1 FROM paper WHERE id = ?", (paper_id,)).fetchone() is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    cid = new_id("ick")
    conn.execute(
        "INSERT INTO integrity_check (id, paper_id, source, checked_on, result, details, notice_url, recorded_by,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (cid, paper_id, source, checked_on or utc_today(), result, details, notice_url, actor.name, utc_now()))
    audit.record(conn, actor, "paper.integrity_check", "paper", paper_id,
                 {"source": source, "result": result, "checked_on": checked_on or utc_today()})
    return cid


def integrity_statement(checks: list[dict]) -> str:
    """Wording for reports: 'none found in <source> on <date>', never 'not retracted'."""
    if not checks:
        return "correction/retraction status not checked"
    parts = []
    for c in sorted(checks, key=lambda c: c["checked_on"], reverse=True):
        if c["result"] == "none_found":
            parts.append(f"no correction or retraction notice found in {c['source']} on {c['checked_on']}")
        else:
            parts.append(f"{c['result'].replace('_', ' ')} reported by {c['source']} (checked {c['checked_on']})")
    return "; ".join(parts)


def set_identity_status(conn: sqlite3.Connection, paper_id: str, status: str, actor: Actor,
                        note: str | None = None) -> None:
    """Reviewer decision on bibliographic identity (separate from content verification)."""
    if status not in ("unverified", "identifier_validated", "reviewer_verified", "conflict"):
        raise ValidationError(f"Unknown identity status {status!r}")
    row = conn.execute("SELECT identity_status FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    conn.execute("UPDATE paper SET identity_status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), paper_id))
    audit.record(conn, actor, "paper.identity_status", "paper", paper_id,
                 {"before": row["identity_status"], "after": status, "note": note})


def get_paper(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any]:
    """A paper with identifiers, stages, conflicts, integrity checks, relations and search memberships."""
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    out = dict(row)
    out["authors"] = loads(row["authors_json"], [])
    out["urls"] = loads(row["urls_json"], [])
    out["other_ids"] = loads(row["other_ids_json"], {})
    q = lambda sql, *p: [dict(r) for r in conn.execute(sql, p)]  # noqa: E731
    out["identifiers"] = q("SELECT * FROM paper_identifier WHERE paper_id = ? ORDER BY scheme, created_at", paper_id)
    out["stages"] = q("SELECT s.key, s.name, ps.assignment_source, ps.status FROM paper_stage ps JOIN stage s"
                      " ON s.id = ps.stage_id WHERE ps.paper_id = ? ORDER BY s.position", paper_id)
    out["metadata_conflicts"] = q("SELECT * FROM metadata_conflict WHERE paper_id = ? ORDER BY created_at", paper_id)
    out["integrity_checks"] = q("SELECT * FROM integrity_check WHERE paper_id = ? ORDER BY checked_on DESC,"
                                " created_at DESC", paper_id)
    out["integrity_statement"] = integrity_statement(out["integrity_checks"])
    out["relations"] = q("SELECT * FROM paper_relation WHERE paper_a = ? OR paper_b = ? ORDER BY created_at",
                         paper_id, paper_id)
    out["search_memberships"] = q(
        "SELECT sr.search_run_id, sr.position, sr.provider_record_id, r.provider, r.mode, r.started_at"
        " FROM search_result sr JOIN search_run r ON r.id = sr.search_run_id WHERE sr.paper_id = ?"
        " ORDER BY r.started_at", paper_id)
    out["documents"] = q("SELECT id, role, format, original_filename, content_scope, record_status FROM document"
                         " WHERE paper_id = ? ORDER BY retrieved_at", paper_id)
    out["source_locations"] = q("SELECT * FROM source_location WHERE owner_type = 'paper' AND owner_id = ? AND"
                                " is_current = 1 ORDER BY role, created_at", paper_id)
    out["members"] = [r["id"] for r in conn.execute("SELECT id FROM paper WHERE canonical_id = ?", (paper_id,))]
    return out


def citation_label(paper: dict | sqlite3.Row) -> str:
    """Short citation for tables: first author + year when known, else the stored citation text."""
    authors = loads(paper["authors_json"], []) if "authors_json" in paper.keys() else paper.get("authors") or []
    if authors and paper["year"]:
        lead = authors[0].split(",")[0].strip()
        return f"{lead}{' et al.' if len(authors) > 1 else ''} ({paper['year']})"
    text = paper["citation_text"] or paper["title"] or paper["id"]
    return text if len(text) <= 80 else text[:79].rstrip() + "…"
