"""Document ingestion: preserved source files, versioned text snapshots, document-to-paper links.

A *document* is one file attached to one paper record (role ``main`` or
``supplementary``). The file is copied byte-for-byte into the project data
directory and never modified. Each parse of the file creates a *snapshot*: the
extracted text units and tables that source locations point at. Snapshots are
never changed or deleted, so a location made against an earlier snapshot keeps
resolving after the document is parsed again.

What attaching a document establishes: that this file was supplied for this
paper record and what text could be extracted from it. It does not verify that
the file is the publication the record describes; identifiers found inside the
file are compared with the record and a disagreement is refused unless the
reviewer overrides it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .. import audit, papers, provenance
from ..db import transaction
from ..errors import NotFoundError, ReviewError, ValidationError
from ..project import Project, preserve_source_file
from ..util import Actor, new_id, sha256_file, utc_now
from . import parsers
from .parsers import ParsedDocument

ROLES = ("main", "supplementary")
CONTENT_SCOPES = ("full_text", "abstract_only", "unknown")
USABLE_STATUSES = ("ok", "partial")
UNAVAILABLE_STATUSES = ("not_available", "abstract_only")
_AVAILABILITY_TEXT = {
    "not_checked": "full text not yet looked for",
    "open_access_reported": "reported as open access by a search source; no document attached yet",
    "document_attached": "a document with extractable text is attached",
    "abstract_only": "only an abstract is available to this project",
    "not_available": "full text could not be obtained",
    "retrieval_failed": "automatic open-access retrieval failed; no document attached",
}


# ------------------------------------------------------------------ helpers
def group_paper_ids(conn: sqlite3.Connection, paper_id: str) -> list[str]:
    """The paper together with the records it is currently grouped with (duplicate grouping)."""
    canon = papers.canonical_id(conn, paper_id)
    return [r["id"] for r in conn.execute("SELECT id FROM paper WHERE id = ? OR canonical_id = ? ORDER BY pk",
                                          (canon, canon))]


def known_identifiers(conn: sqlite3.Connection, paper_id: str) -> dict[str, set[str]]:
    """Identifiers the project holds for a paper and its grouped records (normalised)."""
    ids = group_paper_ids(conn, paper_id)
    marks = ",".join("?" * len(ids))
    out: dict[str, set[str]] = {"doi": set(), "pmid": set(), "pmcid": set()}
    for r in conn.execute(f"SELECT doi, pmid, pmcid FROM paper WHERE id IN ({marks})", ids):
        for scheme in out:
            if r[scheme]:
                out[scheme].add(r[scheme])
    for r in conn.execute(f"SELECT scheme, value_norm FROM paper_identifier WHERE paper_id IN ({marks}) AND active = 1"
                          " AND validation_status != 'invalid_syntax' AND scheme IN ('doi','pmid','pmcid')", ids):
        out[r["scheme"]].add(r["value_norm"])
    return out


def compare_identifiers(known: dict[str, set[str]], found: dict[str, list[str]]) -> dict[str, Any]:
    """``match`` when any identifier agrees, ``mismatch`` when identifiers are present on both sides and none
    agrees, otherwise ``not_checkable``. Pure function."""
    agree, differ = [], []
    for scheme, values in found.items():
        have = known.get(scheme) or set()
        if not values or not have:
            continue
        if have & set(values):
            agree.append(scheme)
        else:
            differ.append({"scheme": scheme, "in_document": list(values), "on_record": sorted(have)})
    outcome = "match" if agree else "mismatch" if differ else "not_checkable"
    return {"outcome": outcome, "agreeing_schemes": agree, "differing": differ if outcome == "mismatch" else []}


def _paper(conn: sqlite3.Connection, paper_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    if row["record_status"] == "removed":
        raise ValidationError(f"Paper {paper_id} is marked removed; documents cannot be attached to it.")
    return row


def _document(conn: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM document WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown document {document_id}")
    return row


def store_snapshot(conn: sqlite3.Connection, document_id: str, parsed: ParsedDocument, now: str) -> dict[str, Any]:
    """Insert one snapshot with its text units and tables. Call inside a transaction."""
    no = conn.execute("SELECT COALESCE(MAX(snapshot_no), 0) + 1 FROM document_snapshot WHERE document_id = ?",
                      (document_id,)).fetchone()[0]
    sid = new_id("snp")
    conn.execute(
        "INSERT INTO document_snapshot (id, document_id, snapshot_no, parser, parser_version, created_at, status,"
        " failure_reason, n_units, n_chars, n_tables, text_sha256, ocr_applied) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (sid, document_id, no, parsed.parser, parsed.parser_version, now, parsed.status, parsed.failure_reason,
         len(parsed.units), parsed.n_chars, len(parsed.tables), parsed.text_sha256))
    conn.executemany(
        "INSERT INTO text_unit (snapshot_id, unit_kind, unit_key, seq, label, section_path, text, extraction_status,"
        " has_images) VALUES (?,?,?,?,?,?,?,?,?)",
        [(sid, u.unit_kind, u.unit_key, u.seq, u.label, u.section_path, u.text, u.extraction_status,
          1 if u.has_images else 0) for u in parsed.units])
    for t in parsed.tables:
        cur = conn.execute("INSERT INTO doc_table (snapshot_id, table_key, seq, label, caption, n_rows, n_cols)"
                           " VALUES (?,?,?,?,?,?,?)", (sid, t.table_key, t.seq, t.label, t.caption, t.n_rows, t.n_cols))
        conn.executemany("INSERT INTO doc_table_cell (table_id, row_idx, col_idx, text, is_header, rowspan, colspan)"
                         " VALUES (?,?,?,?,?,?,?)",
                         [(cur.lastrowid, r, c, text, 1 if h else 0, rs, cs) for r, c, text, h, rs, cs in t.cells])
    return {"snapshot_id": sid, "snapshot_no": no, "status": parsed.status, "failure_reason": parsed.failure_reason,
            "n_units": len(parsed.units), "n_chars": parsed.n_chars, "n_tables": len(parsed.tables),
            "text_sha256": parsed.text_sha256, "parser": parsed.parser, "parser_version": parsed.parser_version}


def latest_snapshot(conn: sqlite3.Connection, document_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM document_snapshot WHERE document_id = ? ORDER BY snapshot_no DESC LIMIT 1",
                       (document_id,)).fetchone()
    return dict(row) if row else None


def _refresh_availability(conn: sqlite3.Connection, paper_id: str, now: str) -> str:
    """Set ``document_attached`` when (and only when) an active main full-text document has usable text."""
    usable = conn.execute(
        "SELECT 1 FROM document d JOIN document_snapshot ds ON ds.document_id = d.id AND ds.snapshot_no ="
        " (SELECT MAX(snapshot_no) FROM document_snapshot WHERE document_id = d.id)"
        " WHERE d.paper_id = ? AND d.record_status = 'active' AND d.role = 'main' AND d.content_scope = 'full_text'"
        " AND ds.status IN ('ok','partial') LIMIT 1", (paper_id,)).fetchone()
    current = conn.execute("SELECT fulltext_availability FROM paper WHERE id = ?", (paper_id,)).fetchone()[0]
    new = current
    if usable:
        new = "document_attached"
    elif current == "document_attached":
        new = "not_checked"
    if new != current:
        conn.execute("UPDATE paper SET fulltext_availability = ?, updated_at = ? WHERE id = ?", (new, now, paper_id))
    return new


# ------------------------------------------------------------------ adding documents
def add_document(project: Project, paper_id: str, path: Path | str, actor: Actor, *, role: str = "main",
                 parent_document_id: str | None = None, declare: str | None = None, source: str = "user_provided",
                 source_url: str | None = None, license: str | None = None, content_scope: str = "full_text",
                 allow_identifier_mismatch: bool = False, original_filename: str | None = None) -> dict[str, Any]:
    """Attach a PDF or XML file to a paper record, preserve it and store its first text snapshot.

    Returns a dict describing the document, its snapshot and the identifier
    check. A file whose text cannot be extracted (scan without text layer,
    unreadable or encrypted file) is still stored, with the snapshot status and
    reason saying so; it does not count as attached full text.
    """
    conn = project.conn
    path = Path(path).expanduser()
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}.")
    if content_scope not in CONTENT_SCOPES:
        raise ValidationError(f"content_scope must be one of {CONTENT_SCOPES}.")
    if not path.is_file():
        raise ValidationError(f"Document file not found: {path}")
    fmt = parsers.detect_format(path)
    if fmt is None:
        raise ValidationError(f"Unsupported document type '{path.suffix}'. Supported: PDF (.pdf) and XML (.xml, .nxml). "
                              "HTML ingestion is not implemented.")
    paper = _paper(conn, paper_id)
    data_class = provenance.resolve_import_class(project.data_class, declare, provenance.detect_embedded_tag(path),
                                                 f"Document '{path.name}'")
    group = group_paper_ids(conn, paper_id)
    if role == "supplementary":
        marks = ",".join("?" * len(group))
        mains = [dict(r) for r in conn.execute(
            f"SELECT * FROM document WHERE paper_id IN ({marks}) AND role = 'main' AND record_status = 'active'", group)]
        if parent_document_id is None:
            if len(mains) != 1:
                raise ValidationError(
                    "A supplementary document must be linked to a main document of the same paper. "
                    f"This paper has {len(mains)} active main document(s); "
                    + ("attach the main document first." if not mains else "name one with parent_document_id."))
            parent_document_id = mains[0]["id"]
        elif parent_document_id not in {m["id"] for m in mains}:
            raise ValidationError(f"{parent_document_id} is not an active main document of this paper "
                                  "(or of a record grouped with it).")
    elif parent_document_id is not None:
        raise ValidationError("Only a supplementary document has a parent document.")

    sha = sha256_file(path)
    existing = conn.execute("SELECT * FROM document WHERE paper_id = ? AND sha256 = ?", (paper_id, sha)).fetchone()
    if existing is not None:
        now = utc_now()
        reactivated = existing["record_status"] == "removed"
        if reactivated:
            with transaction(conn):
                conn.execute("UPDATE document SET record_status = 'active' WHERE id = ?", (existing["id"],))
                _refresh_availability(conn, paper_id, now)
                audit.record(conn, actor, "document.reactivate", "document", existing["id"], {"paper_id": paper_id})
        return {"document_id": existing["id"], "paper_id": paper_id, "already_present": True,
                "reactivated": reactivated, "sha256": sha, "snapshot": latest_snapshot(conn, existing["id"]),
                "identifier_check": None, "also_attached_to": [], "usable_text": None}

    parsed = parsers.parse_document(path, fmt)
    check = compare_identifiers(known_identifiers(conn, paper_id), parsed.identifiers)
    if check["outcome"] == "mismatch" and not allow_identifier_mismatch:
        parts = "; ".join(f"{d['scheme']} in document {', '.join(d['in_document'])} vs record "
                          f"{', '.join(d['on_record'])}" for d in check["differing"])
        raise ValidationError(
            f"The identifiers inside '{path.name}' do not match paper {paper_id} ({parts}). The document was not "
            "attached. Attach it to the matching paper, or override with allow_identifier_mismatch "
            "(--allow-identifier-mismatch) if the record is known to be the same publication.")
    elsewhere = [r["paper_id"] for r in conn.execute(
        "SELECT paper_id FROM document WHERE sha256 = ? AND record_status = 'active'", (sha,))
        if r["paper_id"] not in group]

    stored, sha, size = preserve_source_file(path, project.documents_dir)
    now = utc_now()
    doc_id = new_id("doc")
    with transaction(conn):
        conn.execute(
            "INSERT INTO document (id, paper_id, parent_document_id, role, format, original_filename, sha256,"
            " size_bytes, stored_path, source, source_url, license, retrieved_at, declared_data_class, content_scope,"
            " record_status, added_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (doc_id, paper_id, parent_document_id, role, fmt, original_filename or path.name, sha, size,
             project.rel(stored), source, source_url, license or parsed.license, now, data_class, content_scope,
             actor.name))
        snap = store_snapshot(conn, doc_id, parsed, now)
        availability = _refresh_availability(conn, paper_id, now)
        if content_scope == "abstract_only" and availability in ("not_checked", "open_access_reported",
                                                                  "retrieval_failed", "not_available"):
            conn.execute("UPDATE paper SET fulltext_availability = 'abstract_only', updated_at = ? WHERE id = ?",
                         (now, paper_id))
            availability = "abstract_only"
        audit.record(conn, actor, "document.add", "document", doc_id, {
            "paper_id": paper_id, "role": role, "format": fmt, "sha256": sha, "size_bytes": size, "source": source,
            "license": license or parsed.license, "declared_data_class": data_class, "content_scope": content_scope,
            "snapshot_id": snap["snapshot_id"], "snapshot_status": snap["status"],
            "identifier_check": check, "identifier_mismatch_overridden": check["outcome"] == "mismatch",
            "also_attached_to": elsewhere})
    return {"document_id": doc_id, "paper_id": paper_id, "already_present": False, "reactivated": False,
            "role": role, "format": fmt, "sha256": sha, "size_bytes": size, "stored_path": project.rel(stored),
            "license": license or parsed.license, "declared_data_class": data_class, "snapshot": snap,
            "identifier_check": check, "identifiers_in_document": parsed.identifiers,
            "also_attached_to": elsewhere, "usable_text": parsed.status in USABLE_STATUSES,
            "fulltext_availability": availability, "notes": parsed.notes,
            "paper_label": papers.citation_label(paper)}


def reparse(project: Project, document_id: str, actor: Actor) -> dict[str, Any]:
    """Parse the preserved file again into a new snapshot (earlier snapshots stay as they are)."""
    conn = project.conn
    doc = _document(conn, document_id)
    stored = project.abs(doc["stored_path"])
    if not stored.is_file() or sha256_file(stored) != doc["sha256"]:
        raise ValidationError(f"The preserved copy of document {document_id} is missing or no longer matches its "
                              "SHA-256; it was not parsed.")
    parsed = parsers.parse_document(stored, doc["format"])
    previous = latest_snapshot(conn, document_id)
    now = utc_now()
    with transaction(conn):
        snap = store_snapshot(conn, document_id, parsed, now)
        _refresh_availability(conn, doc["paper_id"], now)
        same = previous is not None and previous["text_sha256"] == snap["text_sha256"]
        audit.record(conn, actor, "document.reparse", "document", document_id,
                     {"snapshot_id": snap["snapshot_id"], "snapshot_no": snap["snapshot_no"],
                      "status": snap["status"], "text_identical_to_previous": same,
                      "parser": snap["parser"], "parser_version": snap["parser_version"]})
    return {**snap, "document_id": document_id, "text_identical_to_previous": same,
            "previous_snapshot_id": previous["id"] if previous else None}


def remove_document(conn: sqlite3.Connection, document_id: str, actor: Actor, reason: str) -> dict[str, Any]:
    """Mark a document removed (nothing is deleted; findings keep their locations and history)."""
    if actor.kind not in ("human", "simulated"):
        raise ReviewError("Only a named reviewer can remove a document.")
    if not (reason or "").strip():
        raise ValidationError("A reason is required to remove a document.")
    doc = _document(conn, document_id)
    now = utc_now()
    with transaction(conn):
        conn.execute("UPDATE document SET record_status = 'removed' WHERE id = ?", (document_id,))
        availability = _refresh_availability(conn, doc["paper_id"], now)
        n = conn.execute(
            "SELECT COUNT(DISTINCT sl.owner_id) FROM source_location sl JOIN document_snapshot ds"
            " ON ds.id = sl.snapshot_id WHERE ds.document_id = ? AND sl.owner_type = 'finding' AND sl.is_current = 1",
            (document_id,)).fetchone()[0]
        audit.record(conn, actor, "document.remove", "document", document_id,
                     {"paper_id": doc["paper_id"], "reason": reason.strip(), "findings_citing_document": n})
    return {"document_id": document_id, "record_status": "removed", "findings_citing_document": n,
            "fulltext_availability": availability}


# ------------------------------------------------------------------ full-text availability
def record_fulltext_unavailable(conn: sqlite3.Connection, paper_id: str, actor: Actor, status: str,
                                note: str) -> dict[str, Any]:
    """Record that full text could not be obtained (``not_available``) or that only an abstract exists."""
    if status not in UNAVAILABLE_STATUSES:
        raise ValidationError(f"status must be one of {UNAVAILABLE_STATUSES}.")
    if not (note or "").strip():
        raise ValidationError("State where the full text was looked for (a short note is required).")
    paper = _paper(conn, paper_id)
    if paper["fulltext_availability"] == "document_attached":
        raise ValidationError("A document with extractable text is attached to this paper; remove it first if it "
                              "is not the full text.")
    now = utc_now()
    with transaction(conn):
        conn.execute("UPDATE paper SET fulltext_availability = ?, updated_at = ? WHERE id = ?", (status, now, paper_id))
        audit.record(conn, actor, "paper.fulltext_availability", "paper", paper_id,
                     {"from": paper["fulltext_availability"], "to": status, "note": note.strip()})
    return {"paper_id": paper_id, "fulltext_availability": status}


def fulltext_status(conn: sqlite3.Connection, paper_id: str) -> dict[str, Any]:
    """What the project holds as full text for a paper, in words that do not overstate it."""
    paper = conn.execute("SELECT id, fulltext_availability FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    docs = list_documents(conn, paper_id=paper_id, include_group=True)
    active = [d for d in docs if d["record_status"] == "active"]
    usable = [d for d in active if d["snapshot_status"] in USABLE_STATUSES]
    problems = [f"{d['id']} ({d['original_filename']}): {d['snapshot_status']}"
                + (f" - {d['failure_reason']}" if d["failure_reason"] else "")
                for d in active if d["snapshot_status"] != "ok"]
    text = _AVAILABILITY_TEXT[paper["fulltext_availability"]]
    if active and not usable:
        text += f"; {len(active)} attached document(s) without usable text"
    return {"paper_id": paper_id, "availability": paper["fulltext_availability"], "statement": text,
            "documents": len(active), "documents_with_usable_text": len(usable), "problems": problems}


# ------------------------------------------------------------------ reading
def list_documents(conn: sqlite3.Connection, paper_id: str | None = None, *, include_group: bool = False,
                   include_removed: bool = True) -> list[dict]:
    where, params = [], []
    if paper_id is not None:
        ids = group_paper_ids(conn, paper_id) if include_group else [paper_id]
        where.append(f"d.paper_id IN ({','.join('?' * len(ids))})")
        params += ids
    if not include_removed:
        where.append("d.record_status = 'active'")
    sql = ("SELECT d.*, ds.id AS snapshot_id, ds.snapshot_no, ds.status AS snapshot_status, ds.failure_reason,"
           " ds.n_units, ds.n_chars, ds.n_tables, ds.parser, ds.parser_version, ds.ocr_applied"
           " FROM document d LEFT JOIN document_snapshot ds ON ds.document_id = d.id AND ds.snapshot_no ="
           " (SELECT MAX(snapshot_no) FROM document_snapshot WHERE document_id = d.id)"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY d.retrieved_at, d.id")
    return [dict(r) for r in conn.execute(sql, params)]


def get_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any]:
    doc = dict(_document(conn, document_id))
    doc["snapshots"] = [dict(r) for r in conn.execute(
        "SELECT * FROM document_snapshot WHERE document_id = ? ORDER BY snapshot_no", (document_id,))]
    doc["supplements"] = [r["id"] for r in conn.execute(
        "SELECT id FROM document WHERE parent_document_id = ? ORDER BY retrieved_at", (document_id,))]
    return doc


def snapshot_info(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    """Snapshot joined with its document (format, role, paper)."""
    row = conn.execute(
        "SELECT ds.*, d.paper_id, d.format, d.role, d.content_scope, d.record_status AS document_status,"
        " d.original_filename FROM document_snapshot ds JOIN document d ON d.id = ds.document_id WHERE ds.id = ?",
        (snapshot_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown document snapshot {snapshot_id}")
    return dict(row)


def resolve_snapshot(conn: sqlite3.Connection, ref: str) -> str:
    """Accept a snapshot id or a document id (meaning its latest snapshot)."""
    if conn.execute("SELECT 1 FROM document_snapshot WHERE id = ?", (ref,)).fetchone():
        return ref
    snap = latest_snapshot(conn, ref) if conn.execute("SELECT 1 FROM document WHERE id = ?", (ref,)).fetchone() \
        else None
    if snap is None:
        raise NotFoundError(f"No document or snapshot with id {ref}")
    return snap["id"]


def unit_location_kind(fmt: str, unit_kind: str) -> str:
    if unit_kind == "page":
        return "pdf_page"
    return "html_element" if fmt == "html" else "xml_element"


def units(conn: sqlite3.Connection, snapshot_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM text_unit WHERE snapshot_id = ? ORDER BY seq", (snapshot_id,))]


def tables(conn: sqlite3.Connection, snapshot_id: str) -> list[dict]:
    out = []
    for t in conn.execute("SELECT * FROM doc_table WHERE snapshot_id = ? ORDER BY seq", (snapshot_id,)):
        table = dict(t)
        table["cells"] = [dict(c) for c in conn.execute(
            "SELECT row_idx, col_idx, text, is_header, rowspan, colspan FROM doc_table_cell WHERE table_id = ?"
            " ORDER BY row_idx, col_idx", (t["id"],))]
        out.append(table)
    return out


def find_text(conn: sqlite3.Connection, snapshot_id: str, needle: str, *, limit: int = 50) -> list[dict]:
    """Exact occurrences of ``needle`` in a snapshot, as ready-made source locations.

    Matching is literal and case-sensitive on the stored text (for PDF pages a
    line break in the stored text matches a space in ``needle``).
    """
    if not needle:
        raise ValidationError("Nothing to search for.")
    info = snapshot_info(conn, snapshot_id)
    hits: list[dict] = []

    def scan(text: str, base: dict) -> None:
        view = text.replace("\n", " ")
        start = view.find(needle)
        while start != -1 and len(hits) < limit:
            end = start + len(needle)
            hits.append({**base, "char_start": start, "char_end": end, "excerpt": text[start:end],
                         "context": text[max(0, start - 60): end + 60]})
            start = view.find(needle, end)

    for u in conn.execute("SELECT * FROM text_unit WHERE snapshot_id = ? AND extraction_status = 'ok' ORDER BY seq",
                          (snapshot_id,)):
        scan(u["text"], {"kind": unit_location_kind(info["format"], u["unit_kind"]), "snapshot_id": snapshot_id,
                         "unit_key": u["unit_key"], "section_path": u["section_path"]})
    for c in conn.execute(
            "SELECT t.table_key, c.row_idx, c.col_idx, c.text FROM doc_table t JOIN doc_table_cell c ON c.table_id ="
            " t.id WHERE t.snapshot_id = ? ORDER BY t.seq, c.row_idx, c.col_idx", (snapshot_id,)):
        scan(c["text"], {"kind": "table_cell", "snapshot_id": snapshot_id, "table_key": c["table_key"],
                         "row_idx": c["row_idx"], "col_idx": c["col_idx"]})
    return hits


def identify(project: Project, path: Path | str) -> dict[str, Any]:
    """Read the identifiers inside a file and list the paper records that carry them. Changes nothing."""
    path = Path(path).expanduser()
    fmt = parsers.detect_format(path)
    if fmt is None or not path.is_file():
        raise ValidationError(f"Not a readable PDF or XML file: {path}")
    parsed = parsers.parse_document(path, fmt)
    conn = project.conn
    matches: dict[str, dict] = {}
    for scheme, values in parsed.identifiers.items():
        for value in values:
            for r in conn.execute(
                    "SELECT DISTINCT p.id, p.title, p.citation_text, p.year, p.authors_json, p.canonical_id FROM paper p"
                    " LEFT JOIN paper_identifier pi ON pi.paper_id = p.id AND pi.active = 1"
                    f" WHERE p.record_status != 'removed' AND (p.{scheme} = ? OR (pi.scheme = ? AND pi.value_norm = ?))",
                    (value, scheme, value)):
                m = matches.setdefault(r["id"], {"paper_id": r["id"], "label": papers.citation_label(r),
                                                 "grouped_under": r["canonical_id"], "matched": []})
                if f"{scheme}:{value}" not in m["matched"]:
                    m["matched"].append(f"{scheme}:{value}")
    return {"file": path.name, "format": fmt, "sha256": sha256_file(path), "parse_status": parsed.status,
            "failure_reason": parsed.failure_reason, "identifiers_in_document": parsed.identifiers,
            "embedded_tag": provenance.detect_embedded_tag(path), "matching_papers": list(matches.values())}
