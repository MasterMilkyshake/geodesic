"""Deterministic detection of candidate numerical values in stored document text.

The detector runs the same rule-based parser as the workbook importer
(:mod:`geodesic.textparse`) over every text unit and every non-header table
cell of a document snapshot. It uses no model and no network, and the same
snapshot always gives the same candidates.

A candidate is a *pointer*: "there is a number at this location". It is not a
finding and it is not evidence that the number means anything. Candidates
never appear in evidence tables, maps or reports. A reviewer either promotes a
candidate (which creates an unreviewed finding through the grounded manual
extraction path) or dismisses it.

PDF page text is hard-wrapped. For sentence splitting only, a line break that
looks like a wrap is read as a space; the stored text is not changed and all
offsets refer to it. A line break is kept as a boundary when the line ends a
sentence (``. ! ? :``), when the line is short (a heading or list item), or
when the next line is empty.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import audit, findings as F, locations, review
from ..db import transaction
from ..documents import store
from ..errors import NotFoundError, ReviewError, ValidationError
from ..project import Project
from ..quantities import units
from ..textparse import PARSER_VERSION, Quantity, check_transcription, parse_text, split_segments
from ..util import Actor, canonical_json, loads, new_id, utc_now
from ..workbook.extract import _time_span, measurement_content
from . import manual

DETECTOR = "geodesic-numeric-candidates"
DETECTOR_VERSION = "1.0"
CONTEXT = "candidate_detection"
HEADER_UNIT_ROLE = "context:unit_original"
SHORT_LINE_CHARS = 45
_OFFSET_KEYS = ("expr_start", "expr_end", "value_start", "value_end", "unit_start", "unit_end",
                "uncertainty_start", "uncertainty_end", "reference_start", "reference_end")


def wrap_view(text: str) -> str:
    """Page text with wrap-like line breaks replaced by spaces (same length, same offsets)."""
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines[:-1]):
        stripped = line.rstrip()
        nxt = lines[i + 1].strip()
        hard = (not stripped or not nxt or stripped[-1] in ".!?:" or len(stripped) <= SHORT_LINE_CHARS)
        out.append(line + ("\n" if hard else " "))
    out.append(lines[-1])
    return "".join(out)


def _quantity_json(q: Quantity, off: int) -> dict[str, Any]:
    d = q.to_dict()
    for k in _OFFSET_KEYS:
        if d.get(k) is not None:
            d[k] += off
    d["qualifiers"] = [{**x, "start": off + x["start"], "end": off + x["end"]} for x in q.qualifiers]
    if not d["qualifiers"]:
        d.pop("qualifiers")
    return d


def _context(seg_start: int, seg_end: int, s: int, e: int) -> tuple[int, int]:
    """The containing segment, cut down around the expression when it is longer than an excerpt may be."""
    limit = locations.EXCERPT_MAX_CHARS
    if seg_end - seg_start <= limit:
        return seg_start, seg_end
    room = max(0, limit - (e - s))
    cs = max(seg_start, s - room // 2)
    ce = min(seg_end, cs + limit)
    return max(seg_start, ce - limit), ce


def _scan(text: str, view: str, phrases: tuple[str, ...], on_semicolon: bool) -> tuple[list[dict], list[dict]]:
    """Candidates and transcription problems in one stored text. Offsets address ``text``."""
    found: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for seg in split_segments(view, on_semicolon):
        p = parse_text(seg.text, phrases)
        if not p.quantities:
            continue
        times = [{"literal": text[seg.start + a:seg.start + b], "char_start": seg.start + a, "char_end": seg.start + b,
                  "value": t.value_num, "unit": t.unit_literal}
                 for t in p.timepoints if t.unit_end is not None for a, b in [_time_span(t)]]
        sizes = [{"literal": text[seg.start + n.start:seg.start + n.end], "char_start": seg.start + n.start,
                  "char_end": seg.start + n.end, "value": n.value, "replicate_type": n.replicate_type,
                  "from_count_phrase": n.from_count_phrase} for n in p.sample_sizes]
        for q in p.quantities:
            s, e = seg.start + q.expr_start, seg.start + q.expr_end
            vs, ve = seg.start + q.value_start, seg.start + q.value_end
            # The parser read the wrap view; the literal is whatever the stored text holds at the span.
            if not check_transcription(view, vs, ve, q.value_literal):
                problems.append({"field": "value_literal", "claimed_literal": q.value_literal, "char_start": vs,
                                 "char_end": ve, "reason": "parser literal is not the stored text at its span"})
                continue
            cs, ce = _context(seg.start, seg.end, s, e)
            found.append({"char_start": s, "char_end": e, "literal": text[s:e], "context_start": cs,
                          "context_end": ce, "context_excerpt": text[cs:ce],
                          "parsed": {"quantity": _quantity_json(q, seg.start), "timepoints": times,
                                     "sample_sizes": sizes, "quantities_in_context": len(p.quantities),
                                     "has_unit": q.unit_literal is not None,
                                     "parser_version": PARSER_VERSION}})
    return found, problems


def detect(project: Project, snapshot_ref: str, actor: Actor) -> dict[str, Any]:
    """Detect candidate numerical values in a document snapshot. Running it again changes nothing."""
    conn = project.conn
    snapshot_id = store.resolve_snapshot(conn, snapshot_ref)
    info = store.snapshot_info(conn, snapshot_id)
    existing = conn.execute("SELECT COUNT(*) FROM numeric_candidate WHERE snapshot_id = ? AND detector = ? AND"
                            " detector_version = ?", (snapshot_id, DETECTOR, DETECTOR_VERSION)).fetchone()[0]
    done = conn.execute(
        "SELECT 1 FROM audit_event WHERE action = 'candidates.detect' AND entity_id = ? AND"
        " json_extract(details_json, '$.detector_version') = ?", (snapshot_id, DETECTOR_VERSION)).fetchone()
    if existing or done:
        return {"snapshot_id": snapshot_id, "paper_id": info["paper_id"], "already_detected": True,
                "candidates": existing, "new": 0, "detector": DETECTOR, "detector_version": DETECTOR_VERSION}
    if info["status"] not in store.USABLE_STATUSES:
        raise ValidationError(f"Snapshot {snapshot_id} has no usable text (status '{info['status']}'"
                              f"{': ' + info['failure_reason'] if info['failure_reason'] else ''}).")
    phrases = manual.parser_phrases(project)
    on_semicolon = project.config.parser.segment_on_semicolon
    now = utc_now()
    rows: list[tuple] = []
    failures: list[tuple[dict, list[dict]]] = []
    by_kind: dict[str, int] = {}
    with_unit = [0]

    def add(kind: str, unit_key: str | None, table: tuple | None, text: str, view: str, extra: dict | None) -> None:
        found, problems = _scan(text, view, phrases, on_semicolon)
        loc = {"kind": kind, "snapshot_id": snapshot_id, "unit_key": unit_key}
        if problems:
            failures.append((loc, problems))
        for c in found:
            parsed = {**c["parsed"], **(extra or {})}
            rows.append((new_id("cnd"), snapshot_id, info["paper_id"], kind, unit_key,
                         table[0] if table else None, table[1] if table else None, table[2] if table else None,
                         c["char_start"], c["char_end"], c["literal"], canonical_json(parsed), c["context_start"],
                         c["context_end"], c["context_excerpt"], DETECTOR, DETECTOR_VERSION, "open", now))
            by_kind[kind] = by_kind.get(kind, 0) + 1
            with_unit[0] += 1 if parsed.get("has_unit") else 0

    for u in conn.execute("SELECT unit_kind, unit_key, text FROM text_unit WHERE snapshot_id = ? AND"
                          " extraction_status = 'ok' ORDER BY seq", (snapshot_id,)):
        kind = store.unit_location_kind(info["format"], u["unit_kind"])
        text = u["text"]
        add(kind, u["unit_key"], None, text, wrap_view(text) if u["unit_kind"] == "page" else text, None)
    for t in conn.execute("SELECT id, table_key FROM doc_table WHERE snapshot_id = ? ORDER BY seq", (snapshot_id,)):
        cells = conn.execute("SELECT row_idx, col_idx, text, is_header FROM doc_table_cell WHERE table_id = ?"
                             " ORDER BY row_idx, col_idx", (t["id"],)).fetchall()
        col_head: dict[int, str] = {}
        for c in cells:
            if c["is_header"]:
                col_head[c["col_idx"]] = (col_head.get(c["col_idx"], "") + " " + (c["text"] or "")).strip()
        row_head = {c["row_idx"]: c["text"] for c in cells if c["col_idx"] == 0 and not c["is_header"]}
        for c in cells:
            if c["is_header"] or not c["text"]:
                continue
            extra = {"column_header": col_head.get(c["col_idx"]), "row_header": row_head.get(c["row_idx"])
                     if c["col_idx"] else None}
            add("table_cell", None, (t["table_key"], c["row_idx"], c["col_idx"]), c["text"], c["text"], extra)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO numeric_candidate (id, snapshot_id, paper_id, location_kind, unit_key, table_key, row_idx,"
            " col_idx, char_start, char_end, literal, parsed_json, context_start, context_end, context_excerpt,"
            " detector, detector_version, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        for loc, problems in failures:
            F.log_parse_failures(conn, CONTEXT, loc, problems)
        audit.record(conn, actor, "candidates.detect", "document_snapshot", snapshot_id, {
            "paper_id": info["paper_id"], "detector": DETECTOR, "detector_version": DETECTOR_VERSION,
            "parser_version": PARSER_VERSION, "candidates": len(rows), "by_location_kind": by_kind,
            "parse_failures": sum(len(p) for _, p in failures)})
    return {"snapshot_id": snapshot_id, "paper_id": info["paper_id"], "already_detected": False,
            "candidates": len(rows), "new": len(rows), "by_location_kind": by_kind,
            "with_unit": with_unit[0],
            "parse_failures": sum(len(p) for _, p in failures), "detector": DETECTOR,
            "detector_version": DETECTOR_VERSION}


# ------------------------------------------------------------------ reading
def _row(r: sqlite3.Row) -> dict[str, Any]:
    out = dict(r)
    out["parsed"] = loads(out.pop("parsed_json"), {})
    out["location"] = candidate_location(out)
    out["address"] = locations.describe(out["location"])
    return out


def candidate_location(c: dict[str, Any], context: bool = False) -> dict[str, Any]:
    """The candidate's source location (the expression, or its context sentence)."""
    s, e = (c["context_start"], c["context_end"]) if context else (c["char_start"], c["char_end"])
    return {"role": F.PRIMARY_ROLE, "kind": c["location_kind"], "snapshot_id": c["snapshot_id"],
            "unit_key": c["unit_key"], "table_key": c["table_key"], "row_idx": c["row_idx"], "col_idx": c["col_idx"],
            "char_start": s, "char_end": e, "excerpt": c["context_excerpt"] if context else c["literal"]}


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    r = conn.execute("SELECT * FROM numeric_candidate WHERE id = ?", (candidate_id,)).fetchone()
    if r is None:
        raise NotFoundError(f"Unknown candidate {candidate_id}")
    return _row(r)


def list_candidates(conn: sqlite3.Connection, *, snapshot_id: str | None = None, paper_id: str | None = None,
                    status: str | None = "open", with_unit_only: bool = False, limit: int | None = 200,
                    offset: int = 0) -> list[dict[str, Any]]:
    """Candidates in document order.

    ``with_unit_only`` hides bare numbers in running text (section numbers, years, counts). Table
    cells are kept, because a table states its unit in the column header, not in the cell.
    """
    sql, params = "SELECT * FROM numeric_candidate WHERE 1 = 1", []
    if snapshot_id:
        sql += " AND snapshot_id = ?"
        params.append(snapshot_id)
    if paper_id:
        ids = store.group_paper_ids(conn, paper_id)
        sql += f" AND paper_id IN ({','.join('?' * len(ids))})"
        params += ids
    if status:
        sql += " AND status = ?"
        params.append(status)
    if with_unit_only:
        sql += " AND (json_extract(parsed_json, '$.has_unit') = 1 OR location_kind = 'table_cell')"
    sql += " ORDER BY snapshot_id, rowid"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return [_row(r) for r in conn.execute(sql, params)]


def counts(conn: sqlite3.Connection, snapshot_id: str | None = None) -> dict[str, int]:
    sql, params = "SELECT status, COUNT(*) n FROM numeric_candidate", []
    if snapshot_id:
        sql += " WHERE snapshot_id = ?"
        params.append(snapshot_id)
    out = {"open": 0, "promoted": 0, "dismissed": 0}
    out.update({r["status"]: r["n"] for r in conn.execute(sql + " GROUP BY status", params)})
    return out


# ------------------------------------------------------------------ resolving
def _open_candidate(conn: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    c = get_candidate(conn, candidate_id)
    if c["status"] != "open":
        raise ReviewError(f"Candidate {candidate_id} is already '{c['status']}'.")
    return c


def proposal(project: Project, candidate_id: str) -> dict[str, Any]:
    """The draft content a promotion would store, re-read from the stored text. Nothing is stored."""
    conn = project.conn
    c = get_candidate(conn, candidate_id)
    ctx = candidate_location(c, context=True)
    for prop in manual.propose_from_span(project, ctx):
        s, e = prop["spans"].get("value_literal", (None, None))
        if s is not None and c["char_start"] <= s and e <= c["char_end"]:
            extra: list[dict[str, Any]] = []
            if c["location_kind"] == "table_cell" and not prop["content"].get("unit_original"):
                header = header_unit(conn, c)
                if header:
                    extra.append(header["location"])
                    prop["content"]["unit_original"] = header["unit_literal"]
                    prop["spans"]["unit_original"] = {"role": HEADER_UNIT_ROLE, "start": header["start"],
                                                      "end": header["end"]}
                    prop["field_status"].pop("unit_original", None)
                    prop["flags"] = [f for f in prop["flags"] if f != "no_unit_detected"] + ["unit_from_column_header"]
            return {**prop, "location": ctx, "extra_locations": extra, "candidate": c}
    raise ValidationError(f"Candidate {candidate_id} can no longer be re-read from its context; "
                          "extract the finding manually from the same location.")


def header_unit(conn: sqlite3.Connection, c: dict[str, Any]) -> dict[str, Any] | None:
    """The unit written in parentheses in the single header cell above a table-cell candidate, if any.

    Only the plain case is read: exactly one header cell in the candidate's column, without column
    span, containing ``(<unit> ...)`` where the unit is in the unit table. Anything else returns
    ``None`` and the reviewer states the unit with its own source location.
    """
    cells = conn.execute(
        "SELECT c.row_idx, c.text, c.colspan FROM doc_table_cell c JOIN doc_table t ON t.id = c.table_id"
        " WHERE t.snapshot_id = ? AND t.table_key = ? AND c.col_idx = ? AND c.is_header = 1",
        (c["snapshot_id"], c["table_key"], c["col_idx"])).fetchall()
    if len(cells) != 1 or (cells[0]["colspan"] or 1) != 1 or not cells[0]["text"]:
        return None
    text = cells[0]["text"]
    found = []
    for i, ch in enumerate(text):
        if ch == "(":
            m = units.match_unit_at(text, i + 1)
            if m:
                found.append(m)
    if len(found) != 1:
        return None
    _, start, end = found[0]
    return {"unit_literal": text[start:end], "start": start, "end": end,
            "location": {"role": HEADER_UNIT_ROLE, "kind": "table_cell", "snapshot_id": c["snapshot_id"],
                         "table_key": c["table_key"], "row_idx": cells[0]["row_idx"], "col_idx": c["col_idx"],
                         "char_start": 0, "char_end": len(text), "excerpt": text}}


def promote(project: Project, candidate_id: str, actor: Actor, *, changes: dict[str, Any] | None = None,
            stage_keys: list[str] | None = None, assignments: dict[str, Any] | None = None,
            paper_id: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Turn a candidate into an unreviewed finding. Returns the stored finding.

    The finding's wording is the candidate's context sentence in the stored text;
    value, unit, uncertainty, timepoint and sample size are the parser's reading
    of that sentence, each grounded at its own span. ``changes`` sets descriptive
    fields (outcome, condition, system context ...) or drops a parsed field by
    setting it to ``None``. The finding still has to be verified in review.
    """
    conn = project.conn
    review._reviewer_actor(conn, actor, "edit")
    c = _open_candidate(conn, candidate_id)
    prop = proposal(project, candidate_id)
    content, spans = dict(prop["content"]), dict(prop["spans"])
    status = dict(prop["field_status"])
    for k, v in (changes or {}).items():
        if k in F.LITERAL_FIELDS and v != content.get(k):
            spans.pop(k, None)              # a re-transcribed literal is located again inside the excerpt
        content[k] = v
    if (changes or {}).get("field_status"):
        status.update(changes["field_status"])
    for key, fld in manual._MEASUREMENT_DEFAULT_STATUS:
        if content.get(fld) not in (None, ""):
            status.pop(key, None)
    content["field_status"] = status
    flags = list(prop["flags"])
    with transaction(conn):
        finding = manual.create_finding(
            project, paper_id or c["paper_id"], actor, location=prop["location"], content=content, spans=spans,
            extra_locations=prop["extra_locations"] if content.get("unit_original") and isinstance(
                spans.get("unit_original"), dict) else None,
            stage_keys=stage_keys, assignments=assignments, flags=flags, note=note, origin="candidate_promotion",
            extraction_method="deterministic_parser+reviewer_promotion", change_kind="candidate_promotion")
        now = utc_now()
        conn.execute("UPDATE numeric_candidate SET status = 'promoted', finding_id = ?, resolved_at = ?,"
                     " resolved_by = ? WHERE id = ?", (finding["id"], now, actor.name, candidate_id))
        audit.record(conn, actor, "candidate.promote", "numeric_candidate", candidate_id,
                     {"finding_id": finding["id"], "snapshot_id": c["snapshot_id"], "address": c["address"],
                      "flags": flags})
    return finding


def dismiss(conn: sqlite3.Connection, candidate_ids: list[str], actor: Actor, note: str | None = None) -> int:
    """Mark candidates as not being evidence (page numbers, years, identifiers ...). Kept in the record."""
    review._reviewer_actor(conn, actor, "edit")
    now = utc_now()
    with transaction(conn):
        for cid in candidate_ids:
            _open_candidate(conn, cid)
            conn.execute("UPDATE numeric_candidate SET status = 'dismissed', resolved_at = ?, resolved_by = ?"
                         " WHERE id = ?", (now, actor.name, cid))
        audit.record(conn, actor, "candidate.dismiss", "numeric_candidate", candidate_ids[0] if candidate_ids else "",
                     {"candidate_ids": candidate_ids, "count": len(candidate_ids), "note": note})
    return len(candidate_ids)
