"""Manual evidence extraction from stored document text, anchored to a source location.

A reviewer selects a span of stored text (a page, an XML element or a table
cell of a document snapshot). The finding's ``source_wording`` *is* the stored
text at that span; it is never typed in. Every transcribed literal (value, unit,
uncertainty, timepoint, sample size) must be found at a stated position inside
that span. A proposed transcription whose claimed source string is absent from
the specified span is refused, and the refusal is logged as a parse failure.

Numbers must agree with the literal they were transcribed from: a numeric field
may only hold a number that is written in its literal.

A finding created here starts as ``unreviewed`` (or ``draft`` when the
deterministic parser flagged an ambiguity). Creating a finding is not verifying
it; verification is the separate review step in :mod:`geodesic.review`.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .. import audit, findings as F, locations, review
from ..db import transaction
from ..documents import store
from ..errors import GroundingError, NotFoundError, ValidationError
from ..project import Project
from ..textparse import DEFAULT_QUALIFIER_PHRASES, number_from_literal, parse_text, split_segments
from ..util import Actor
from ..workbook.extract import measurement_content, _timepoint

CONTEXT = "manual_extraction"
_NUMBER_RE = re.compile(r"[-+−]?\d[\d,]*(?:\.\d+)?(?:\s?[×x]\s?10\^?[-+−]?[\d⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?")
_NUMERIC_FIELDS = {"value_literal": ("value_num", "value_low", "value_high"),
                   "uncertainty_literal": ("uncertainty_value", "uncertainty_low", "uncertainty_high"),
                   "timepoint_literal": ("timepoint_value",), "sample_size_literal": ("sample_size",)}
_MEASUREMENT_DEFAULT_STATUS = (("unit_original", "unit_original"), ("uncertainty", "uncertainty_type"),
                               ("timepoint", "timepoint_literal"), ("sample_size", "sample_size"),
                               ("outcome", "outcome"), ("observation_kind", "observation_kind"),
                               ("observation_type", "observation_type"))


def parser_phrases(project: Project) -> tuple[str, ...]:
    return tuple(dict.fromkeys([*DEFAULT_QUALIFIER_PHRASES, *project.config.parser.extra_qualifier_phrases]))


# ------------------------------------------------------------------ locations
def build_location(conn: sqlite3.Connection, snapshot_ref: str, *, unit_key: str | None = None,
                   table_key: str | None = None, row_idx: int | None = None, col_idx: int | None = None,
                   char_start: int | None = None, char_end: int | None = None, quote: str | None = None,
                   role: str = F.PRIMARY_ROLE) -> dict[str, Any]:
    """A resolved source location in a document snapshot.

    Give either a character span or ``quote`` (the exact text, which must occur
    exactly once in the unit unless ``char_start`` picks the occurrence). With
    neither, the location covers the whole unit. The excerpt is read from the
    stored text; it is never taken from the caller.
    """
    snapshot_id = store.resolve_snapshot(conn, snapshot_ref)
    info = store.snapshot_info(conn, snapshot_id)
    loc: dict[str, Any] = {"role": role, "snapshot_id": snapshot_id}
    if table_key is not None:
        if row_idx is None or col_idx is None:
            raise ValidationError("A table cell location needs table_key, row_idx and col_idx.")
        loc.update(kind="table_cell", table_key=table_key, row_idx=int(row_idx), col_idx=int(col_idx))
    else:
        if unit_key is None:
            raise ValidationError("A location needs a unit key (page index or element key) or a table cell.")
        unit = conn.execute("SELECT unit_kind FROM text_unit WHERE snapshot_id = ? AND unit_key = ?",
                            (snapshot_id, str(unit_key))).fetchone()
        if unit is None:
            raise GroundingError(f"Snapshot {snapshot_id} has no text unit '{unit_key}'.")
        loc.update(kind=store.unit_location_kind(info["format"], unit["unit_kind"]), unit_key=str(unit_key))
    text, problem = locations.unit_text(conn, loc)
    if text is None:
        raise GroundingError(f"The location does not resolve: {problem}.")
    if quote is not None:
        if not quote:
            raise ValidationError("The quoted text is empty.")
        view, needle = text.replace("\n", " "), quote.replace("\n", " ")
        starts = [m.start() for m in re.finditer(re.escape(needle), view)]
        if char_start is not None:
            starts = [s for s in starts if s == char_start]
        if not starts:
            raise GroundingError("The quoted text does not occur at this location in the stored document text.")
        if len(starts) > 1:
            raise ValidationError(f"The quoted text occurs {len(starts)} times in this unit (at character offsets "
                                  f"{', '.join(map(str, starts))}); give the start offset of the one meant.")
        char_start, char_end = starts[0], starts[0] + len(needle)
    elif char_start is None and char_end is None:
        char_start, char_end = 0, len(text)
    ok, problem = locations.check_span(text, char_start, char_end, None)
    if not ok:
        raise GroundingError(f"The location does not resolve: {problem}.")
    loc.update(char_start=int(char_start), char_end=int(char_end), excerpt=text[char_start:char_end])
    return loc


def _source_scope(info: dict[str, Any]) -> str:
    if info["role"] == "supplementary":
        return "supplementary"
    return "abstract_only" if info["content_scope"] == "abstract_only" else "full_text"


# ------------------------------------------------------------------ deterministic proposals
def propose_from_span(project: Project, loc: dict[str, Any]) -> list[dict[str, Any]]:
    """What the deterministic parser reads in a span: one proposal per quantity, else one qualitative proposal.

    Proposals are drafts for the reviewer. Each carries ``content`` (without
    ``source_wording``), ``spans`` relative to the unit text, ``flags`` and
    ``field_status``. Nothing is stored.
    """
    conn = project.conn
    text, problem = locations.unit_text(conn, loc)
    if text is None:
        raise GroundingError(f"The location does not resolve: {problem}.")
    start = loc.get("char_start") or 0
    end = loc.get("char_end") if loc.get("char_end") is not None else len(text)
    piece = text[start:end]
    view = piece.replace("\n", " ")
    p = parse_text(view, parser_phrases(project))
    quals = [{**x, "start": start + x["start"], "end": start + x["end"]} for x in p.qualifiers]
    out: list[dict[str, Any]] = []
    many = ["multiple_quantities_in_segment"] if len(p.quantities) > 1 else []
    for q in p.quantities:
        c, spans, flags, status = measurement_content(q, p, start, F.blank_content(), view)
        _literals_from_stored_text(c, spans, text)
        out.append({"content": c, "spans": spans, "flags": list(dict.fromkeys([*flags, *many])),
                    "field_status": status, "summary": _summary(c)})
    if not out:
        c = F.blank_content(kind="narrative", statement_type="qualitative", qualifiers=quals)
        spans: dict[str, Any] = {}
        flags: list[str] = []
        status: dict[str, str] = {}
        _timepoint(p, start, view, c, spans, flags, status)
        _literals_from_stored_text(c, spans, text)
        out.append({"content": c, "spans": spans, "flags": flags, "field_status": status, "summary": _summary(c)})
    return out


def draft_from_proposal(props: list[dict[str, Any]], *, quantity: int | None = None,
                        changes: dict[str, Any] | None = None, kind: str | None = None,
                        statuses: dict[str, str] | None = None, spans: dict[str, Any] | None = None,
                        ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Content, spans and flags of a finding a reviewer assembles from an excerpt.

    ``quantity`` (1-based) starts from one of the deterministic readings
    returned by :func:`propose_from_span`; ``changes`` are the reviewer's own
    field values and win over the reading. A changed literal loses the span
    the reading had for it, so it must be grounded again. Used by the CLI and
    the dashboard alike.
    """
    content: dict[str, Any] = {}
    out_spans: dict[str, Any] = {}
    flags: list[str] = []
    if quantity is not None:
        if not 1 <= quantity <= len(props) or props[quantity - 1]["content"].get("value_literal") is None:
            raise ValidationError(f"The excerpt has no parsed quantity number {quantity}; "
                                  "see the numbered readings of the excerpt first.")
        chosen = props[quantity - 1]
        content = {k: v for k, v in chosen["content"].items() if v not in (None, "", [], {})}
        content["field_status"] = dict(chosen["field_status"])
        out_spans, flags = dict(chosen["spans"]), list(chosen["flags"])
    for k, v in (changes or {}).items():
        if k in F.LITERAL_FIELDS and v != content.get(k):
            out_spans.pop(k, None)
        content[k] = v
    if kind:
        content["kind"] = kind
    if statuses:
        content["field_status"] = {**(content.get("field_status") or {}), **statuses}
    out_spans.update(spans or {})
    return content, out_spans, flags


def _literals_from_stored_text(content: dict[str, Any], spans: dict[str, Any], text: str) -> None:
    """Literals are the stored text at their spans (a wrapped PDF line keeps its line break)."""
    for fld in F.LITERAL_FIELDS:
        if content.get(fld) and isinstance(spans.get(fld), (list, tuple)):
            s, e = spans[fld]
            content[fld] = text[s:e]


def _summary(c: dict[str, Any]) -> str:
    if c.get("value_literal") is None:
        return f"{c['kind']} ({c['statement_type']})"
    parts = [c["statement_type"], c["value_literal"]]
    if c.get("uncertainty_literal"):
        parts.append(f"± {c['uncertainty_literal']} [{c.get('uncertainty_type')}]")
    if c.get("unit_original"):
        parts.append(c["unit_original"])
    if c.get("timepoint_literal"):
        parts.append(f"@ {c['timepoint_literal']}")
    if c.get("sample_size_literal"):
        parts.append(f"({c['sample_size_literal']})")
    return " ".join(parts)


# ------------------------------------------------------------------ checks
def _numbers_in(literal: str) -> list[float]:
    values: list[float] = []
    try:
        values.append(number_from_literal(literal))
    except ValueError:
        pass
    for m in _NUMBER_RE.finditer(literal):
        token = m.group(0)
        try:
            values.append(number_from_literal(token))
            if token[0] in "-+−" and m.start() > 0:      # "0.8-1.4": the dash may be a range dash, not a sign
                values.append(number_from_literal(token[1:]))
        except ValueError:
            continue
    return values


def number_problems(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Numeric fields that do not hold a number written in their literal."""
    problems: list[dict[str, Any]] = []
    for literal_field, numeric_fields in _NUMERIC_FIELDS.items():
        literal = content.get(literal_field)
        for nf in numeric_fields:
            value = content.get(nf)
            if value is None:
                continue
            if not literal:
                problems.append({"field": nf, "claimed_literal": None,
                                 "reason": f"{nf} = {value} has no transcribed literal ({literal_field}) behind it"})
                continue
            written = _numbers_in(str(literal))
            if not any(abs(float(value) - w) <= 1e-9 * max(1.0, abs(w)) for w in written):
                problems.append({"field": nf, "claimed_literal": str(literal),
                                 "reason": f"{nf} = {value} is not a number written in {literal_field} "
                                           f"'{literal}'"})
    return problems


def _refuse(conn: sqlite3.Connection, actor: Actor, loc: dict[str, Any], problems: list[dict[str, Any]],
            what: str) -> None:
    with transaction(conn):
        F.log_parse_failures(conn, CONTEXT, loc, problems)
        audit.record(conn, actor, "finding.extraction_refused", "document_snapshot", loc.get("snapshot_id") or "",
                     {"location": locations.describe(loc), "problems": [
                         {k: p.get(k) for k in ("field", "reason", "claimed_literal")} for p in problems]})
    raise GroundingError(f"{what} refused and logged as a parse failure: "
                         + "; ".join(f"{p['field']}: {p['reason']}" for p in problems))


# ------------------------------------------------------------------ creating findings
def create_finding(project: Project, paper_id: str, actor: Actor, *, location: dict[str, Any],
                   content: dict[str, Any] | None = None, spans: dict[str, Any] | None = None,
                   stage_keys: list[str] | None = None, assignments: dict[str, Any] | None = None,
                   extra_locations: list[dict[str, Any]] | None = None, flags: list[str] | None = None,
                   note: str | None = None, origin: str = "manual_extraction", extraction_method: str = "manual",
                   change_kind: str = "manual_create") -> dict[str, Any]:
    """Store one finding extracted from a document location. Returns the stored finding.

    ``location`` comes from :func:`build_location` (kind, snapshot, unit, span).
    ``content`` holds the extraction fields except ``source_wording``. ``spans``
    maps each transcribed literal to its ``[start, end)`` in the unit text; a
    literal without a span is located automatically when it occurs exactly once
    inside the excerpt.
    """
    conn = project.conn
    review._reviewer_actor(conn, actor, "edit")
    paper = conn.execute("SELECT id, record_status FROM paper WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise NotFoundError(f"Unknown paper {paper_id}")
    loc = dict(location)
    loc["role"] = F.PRIMARY_ROLE
    if loc.get("kind") not in locations.DOCUMENT_KINDS:
        raise ValidationError("Manual extraction needs a document location (page, element or table cell).")
    info = store.snapshot_info(conn, loc.get("snapshot_id") or "")
    if info["paper_id"] not in store.group_paper_ids(conn, paper_id):
        raise ValidationError(f"Document snapshot {info['id']} belongs to paper {info['paper_id']}, not to "
                              f"{paper_id} or a record grouped with it.")
    if info["document_status"] != "active":
        raise ValidationError("The document was removed from the project; new findings cannot cite it.")
    text, problem = locations.unit_text(conn, loc)
    if text is None:
        raise GroundingError(f"The location does not resolve: {problem}.")
    if loc.get("char_start") is None:
        loc["char_start"], loc["char_end"] = 0, len(text)
    start, end = loc["char_start"], loc["char_end"]
    ok, problem = locations.check_span(text, start, end, loc.get("excerpt"))
    if not ok:
        _refuse(conn, actor, loc, [{"field": "location:supporting_excerpt", "reason": problem,
                                    "claimed_literal": loc.get("excerpt"), "char_start": start, "char_end": end}],
                "The source location was")
    excerpt = text[start:end]
    loc["excerpt"] = excerpt
    if len(excerpt) > locations.EXCERPT_MAX_CHARS:
        raise ValidationError(f"The supporting excerpt is {len(excerpt)} characters long; the limit is "
                              f"{locations.EXCERPT_MAX_CHARS}. Select the sentence(s) that state the finding.")
    if not excerpt.strip():
        raise ValidationError("The selected span contains no text.")

    given = dict(content or {})
    unknown = [k for k in given if k not in F.CONTENT_KEYS]
    if unknown:
        raise ValidationError(f"Unknown finding fields: {', '.join(unknown)}")
    problems: list[dict[str, Any]] = []
    if given.get("source_wording") not in (None, excerpt):
        problems.append({"field": "source_wording", "claimed_literal": given["source_wording"],
                         "char_start": start, "char_end": end,
                         "reason": "claimed source string is absent from the specified span"})
    c = F.blank_content(**{k: v for k, v in given.items() if k != "source_wording"})
    c["source_wording"] = excerpt
    c["kind"] = c.get("kind") or ("measurement" if c.get("value_literal") else "narrative")
    if not c.get("statement_type"):
        if c.get("value_literal"):
            raise ValidationError("statement_type is required when a value is transcribed "
                                  "(exact, approximate, range or bound).")
        c["statement_type"] = "qualitative"
    c["qualifiers"] = list(c.get("qualifiers") or [])
    status = dict(c.get("field_status") or {})
    if c["kind"] == "measurement":
        for key, fld in _MEASUREMENT_DEFAULT_STATUS:
            if c.get(fld) not in (None, ""):
                status.pop(key, None)   # a transcribed value replaces the missing-information state
            elif key not in status:
                status[key] = "not_yet_extracted"
    c["field_status"] = status

    spans = {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in (spans or {}).items()}
    for fld in F.LITERAL_FIELDS:
        literal = c.get(fld)
        if literal in (None, ""):
            spans.pop(fld, None)
            continue
        if fld not in spans:
            found = [m.start() for m in re.finditer(re.escape(str(literal)), excerpt)]
            whole = [f for f in found if not F.span_cuts_token(excerpt, f, f + len(str(literal)))]
            found = whole or found[:1]      # an occurrence inside a longer number or word is not the literal
            if len(found) == 1:
                spans[fld] = [start + found[0], start + found[0] + len(str(literal))]
            elif not found:
                problems.append({"field": fld, "claimed_literal": str(literal), "char_start": start,
                                 "char_end": end, "reason": "claimed source string is absent from the specified span"})
            else:
                raise ValidationError(f"{fld} '{literal}' occurs {len(found)} times in the excerpt; give its span "
                                      f"(unit offsets {', '.join(str(start + f) for f in found)}).")
        elif isinstance(spans[fld], list) and len(spans[fld]) == 2:
            s, e = spans[fld]
            if s is None or e is None or s < start or e > end:
                problems.append({"field": fld, "claimed_literal": str(literal), "char_start": s, "char_end": e,
                                 "reason": "the literal's span lies outside the supporting excerpt"})
    locs = [loc]
    for extra in extra_locations or []:
        if not extra.get("role") or extra["role"] == F.PRIMARY_ROLE:
            raise ValidationError("Additional locations need a role other than 'supporting_excerpt' "
                                  "(for example 'context:outcome').")
        locs.append(dict(extra))
    if not problems:
        problems = F.grounding_problems(c, spans, F.stored_unit_texts(conn, locs), locs)
    if problems:
        _refuse(conn, actor, loc, problems, "The proposed transcription was")
    numeric = number_problems(c)
    if numeric:
        _refuse(conn, actor, loc, numeric, "The proposed numeric values were")
    c = F.validate_content(c)

    stage_ids = F.stage_id_map(conn)
    stage_keys = list(dict.fromkeys(stage_keys or []))
    missing = [k for k in stage_keys if k not in stage_ids]
    if missing:
        raise ValidationError(f"Unknown stage key(s): {', '.join(missing)}. Stages are defined in project.yaml.")
    assignments = {k: v for k, v in (assignments or {}).items() if v}
    if assignments:
        from . import hierarchy
        hierarchy.check_assignments(conn, paper_id, assignments)
    flags = list(dict.fromkeys(flags or []))
    item = F.NewFinding(
        paper_id=paper_id, origin=origin, extraction_method=extraction_method, content=c, locations=locs,
        spans=spans, stage_keys=stage_keys, stage_source="reviewer",
        review_status="draft" if set(flags) & F.DRAFT_FLAGS else "unreviewed", review_flags=flags,
        grounding_status="grounded", source_scope=_source_scope(info), assignments=assignments)
    with transaction(conn):
        F.insert_findings(conn, [item], actor, change_kind, stage_ids, note)
        audit.record(conn, actor, "finding.create", "finding", item.id, {
            "paper_id": paper_id, "origin": origin, "extraction_method": extraction_method, "kind": c["kind"],
            "statement_type": c["statement_type"], "location": locations.describe(loc),
            "snapshot_id": loc["snapshot_id"], "review_status": item.review_status, "flags": flags,
            "stage_keys": stage_keys, "note": note})
    return F.get_finding(conn, item.id)


def segments_of_unit(conn: sqlite3.Connection, loc: dict[str, Any], on_semicolon: bool = True) -> list[dict]:
    """Sentence-sized spans of a unit, as a help for choosing an excerpt (nothing is stored)."""
    text, problem = locations.unit_text(conn, loc)
    if text is None:
        raise GroundingError(f"The location does not resolve: {problem}.")
    return [{"char_start": s.start, "char_end": s.end, "text": text[s.start:s.end]}
            for s in split_segments(text.replace("\n", " "), on_semicolon)]
