"""Findings: schema validation, versioned storage, grounding and staleness.

A finding always links to a paper and to at least one source location. Its
current content lives in the ``finding`` row; every state it has ever had is
kept as an immutable ``finding_version`` snapshot, so reviewer corrections and
re-imports never erase the original extraction.

Grounding
---------
A finding is *grounded* when its supporting excerpt resolves to stored text and
every transcribed literal (value, unit, uncertainty, timepoint, sample size)
is exactly the stored text at its recorded span. Grounding shows that the
strings are in the source. It does not show that the finding interprets the
source correctly; that is what reviewer verification is for.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import audit, locations
from .errors import GroundingError, NotFoundError, ValidationError
from .util import Actor, canonical_json, loads, new_id, sha256_text, utc_now

FIELD_STATUSES = ("not_reported", "not_applicable", "not_yet_extracted", "could_not_be_parsed")
STATEMENT_TYPES = ("exact", "approximate", "range", "bound", "qualitative", "unparsed")
RELATIVE_SEMANTICS = ("absolute", "percent_of_reference", "percent_of_reference_per_mass_or_volume", "fold_change",
                      "percent_of_counted_population", "arbitrary_units", "unspecified_relative")
OBSERVATION_TYPES = ("direct_measurement", "proxy_signal", "functional_outcome")
UNCERTAINTY_TYPES = ("sd", "se", "ci", "iqr", "range", "unspecified_plus_minus", "other")

#: Content columns of ``finding`` in storage order. JSON columns end in ``_json``.
CONTENT_COLUMNS = (
    "kind", "source_wording", "statement_type", "system_context", "experimental_context", "intervention",
    "comparator", "outcome", "measurement_method", "observation_type", "value_literal", "value_num", "value_low",
    "value_high", "bound_type", "unit_original", "relative_semantics", "reference_definition", "timepoint_literal",
    "timepoint_value", "time_unit", "observation_kind", "summary_statistic", "sample_size", "sample_size_literal",
    "replicate_type", "uncertainty_type", "uncertainty_literal", "uncertainty_value", "uncertainty_low",
    "uncertainty_high", "confidence_level", "denominator", "normalization", "qualifiers_json",
    "interpretation_limits", "field_status_json",
)
#: Content keys as used in Python dictionaries (JSON columns without the suffix).
CONTENT_KEYS = tuple(c[:-5] if c.endswith("_json") else c for c in CONTENT_COLUMNS)
ASSIGNMENT_KEYS = ("study_id", "experiment_id", "comparison_group_id")
#: Transcribed literals that must be grounded in stored text.
LITERAL_FIELDS = ("value_literal", "unit_original", "uncertainty_literal", "timepoint_literal", "sample_size_literal")
#: Fields a reviewer may mark with an explicit missing-information status.
STATUS_FIELDS = ("outcome", "system_context", "experimental_context", "intervention", "comparator",
                 "measurement_method", "observation_type", "value", "unit_original", "timepoint", "sample_size",
                 "uncertainty", "denominator", "normalization", "observation_kind", "reference_definition")
PRIMARY_ROLE = "supporting_excerpt"


class FindingContent(BaseModel):
    """Schema of a finding's content. Used to validate manual extractions and reviewer edits."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["measurement", "narrative", "limitation"]
    source_wording: str = Field(min_length=1)
    statement_type: Literal["exact", "approximate", "range", "bound", "qualitative", "unparsed"]
    system_context: str | None = None
    experimental_context: str | None = None
    intervention: str | None = None
    comparator: str | None = None
    outcome: str | None = None
    measurement_method: str | None = None
    observation_type: Literal["direct_measurement", "proxy_signal", "functional_outcome"] | None = None
    value_literal: str | None = None
    value_num: float | None = None
    value_low: float | None = None
    value_high: float | None = None
    bound_type: Literal["gt", "ge", "lt", "le"] | None = None
    unit_original: str | None = None
    relative_semantics: Literal["absolute", "percent_of_reference", "percent_of_reference_per_mass_or_volume",
                                "fold_change", "percent_of_counted_population", "arbitrary_units",
                                "unspecified_relative"] | None = None
    reference_definition: str | None = None
    timepoint_literal: str | None = None
    timepoint_value: float | None = None
    time_unit: str | None = None
    observation_kind: Literal["single_observation", "summary_statistic"] | None = None
    summary_statistic: str | None = None
    sample_size: int | None = Field(default=None, ge=0)
    sample_size_literal: str | None = None
    replicate_type: str | None = None
    uncertainty_type: Literal["sd", "se", "ci", "iqr", "range", "unspecified_plus_minus", "other"] | None = None
    uncertainty_literal: str | None = None
    uncertainty_value: float | None = None
    uncertainty_low: float | None = None
    uncertainty_high: float | None = None
    confidence_level: float | None = Field(default=None, gt=0, lt=100)
    denominator: str | None = None
    normalization: str | None = None
    qualifiers: list[dict] = Field(default_factory=list)
    interpretation_limits: str | None = None
    field_status: dict[str, Literal["not_reported", "not_applicable", "not_yet_extracted",
                                    "could_not_be_parsed"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> "FindingContent":
        st = self.statement_type
        problems: list[str] = []
        if self.kind in ("narrative", "limitation") and st not in ("qualitative", "unparsed"):
            problems.append(f"a {self.kind} finding must be 'qualitative' or 'unparsed', not '{st}'")
        if st in ("exact", "approximate"):
            if self.value_num is None or self.value_literal is None:
                problems.append(f"statement_type '{st}' needs value_literal and value_num")
            if self.value_low is not None or self.value_high is not None or self.bound_type is not None:
                problems.append(f"statement_type '{st}' must not carry range ends or a bound")
        elif st == "range":
            if self.value_low is None or self.value_high is None or self.value_literal is None:
                problems.append("statement_type 'range' needs value_literal, value_low and value_high")
            if self.value_num is not None:
                problems.append("a range must not be stored as a point value (value_num must be empty)")
            if self.bound_type is not None:
                problems.append("a range must not carry a bound_type")
        elif st == "bound":
            if self.bound_type is None or self.value_literal is None:
                problems.append("statement_type 'bound' needs bound_type and value_literal")
            if self.value_num is not None:
                problems.append("a bound must not be stored as a point value (value_num must be empty)")
            lower = self.bound_type in ("gt", "ge")
            if self.bound_type and ((lower and (self.value_low is None or self.value_high is not None))
                                    or (not lower and (self.value_high is None or self.value_low is not None))):
                problems.append("a lower bound (gt/ge) sets only value_low; an upper bound (lt/le) only value_high")
        else:
            if any(v is not None for v in (self.value_num, self.value_low, self.value_high, self.bound_type)):
                problems.append(f"statement_type '{st}' must not carry numeric values")
        if self.uncertainty_type is None and any(
                v is not None for v in (self.uncertainty_value, self.uncertainty_low, self.uncertainty_high,
                                        self.uncertainty_literal)):
            problems.append("uncertainty values need an uncertainty_type (use 'unspecified_plus_minus' when the "
                            "source does not say what ± means)")
        if self.confidence_level is not None and self.uncertainty_type != "ci":
            problems.append("confidence_level applies only to uncertainty_type 'ci'")
        if self.timepoint_value is not None and not self.time_unit:
            problems.append("timepoint_value needs a time_unit")
        for key in self.field_status:
            if key not in STATUS_FIELDS:
                problems.append(f"field_status key {key!r} is not one of: {', '.join(STATUS_FIELDS)}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


def validate_content(content: dict[str, Any]) -> dict[str, Any]:
    """Validate content against the extraction schema; returns the normalised dictionary."""
    try:
        return FindingContent.model_validate(content).model_dump()
    except Exception as exc:
        raise ValidationError(f"Finding content does not satisfy the extraction schema: {exc}") from exc


def blank_content(**values: Any) -> dict[str, Any]:
    content: dict[str, Any] = {k: None for k in CONTENT_KEYS}
    content["qualifiers"] = []
    content["field_status"] = {}
    content.update(values)
    return content


def content_hash(content: dict[str, Any], stage_keys: Iterable[str]) -> str:
    """Hash of what a finding says, independent of where its source text sits.

    Character positions inside qualifiers are excluded so that moving a row or
    inserting text elsewhere in a cell does not count as a change of evidence.
    """
    body = {k: content.get(k) for k in CONTENT_KEYS if k not in ("qualifiers",)}
    body["qualifiers"] = [{k: v for k, v in q.items() if k not in ("start", "end")}
                          for q in content.get("qualifiers") or []]
    body["stage_keys"] = sorted(stage_keys)
    return sha256_text(canonical_json(body))


def _content_row(content: dict[str, Any]) -> list[Any]:
    row = []
    for col in CONTENT_COLUMNS:
        if col == "qualifiers_json":
            row.append(canonical_json(content.get("qualifiers") or []))
        elif col == "field_status_json":
            row.append(canonical_json(content.get("field_status") or {}))
        else:
            row.append(content.get(col))
    return row


def content_from_row(row: sqlite3.Row | dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, key in zip(CONTENT_COLUMNS, CONTENT_KEYS):
        out[key] = loads(row[col], [] if key == "qualifiers" else {}) if col.endswith("_json") else row[col]
    return out


def _loc_summary(loc: dict[str, Any]) -> dict[str, Any]:
    keys = ("role", "kind", "workbook_version_id", "sheet", "cell_ref", "snapshot_id", "unit_key", "table_key",
            "row_idx", "col_idx", "char_start", "char_end", "excerpt")
    return {k: loc.get(k) for k in keys if loc.get(k) is not None}


def make_snapshot(content: dict[str, Any], stage_keys: Iterable[str], spans: dict, locs: list[dict],
                  review_flags: list[str], assignments: dict[str, Any] | None = None,
                  grounding_status: str = "grounded") -> dict[str, Any]:
    return {"content": {k: content.get(k) for k in CONTENT_KEYS}, "stage_keys": sorted(stage_keys),
            "spans": spans, "locations": [_loc_summary(l) for l in locs], "review_flags": list(review_flags),
            "assignments": {k: (assignments or {}).get(k) for k in ASSIGNMENT_KEYS},
            "grounding_status": grounding_status}


# ------------------------------------------------------------------ grounding
def _span_target(span: Any) -> tuple[str, int | None, int | None]:
    if isinstance(span, dict):
        return span.get("role") or PRIMARY_ROLE, span.get("start"), span.get("end")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        return PRIMARY_ROLE, span[0], span[1]
    return PRIMARY_ROLE, None, None


def grounding_problems(content: dict[str, Any], spans: dict[str, Any], unit_texts: dict[str, str | None],
                       locs: list[dict]) -> list[dict[str, Any]]:
    """Check a proposed finding against stored source text. Returns a list of problems (empty = grounded).

    ``unit_texts`` maps a location role to the full stored text of the unit that
    location addresses. ``locs`` are the proposed locations (with excerpts).
    """
    problems: list[dict[str, Any]] = []
    primary = [l for l in locs if (l.get("role") or PRIMARY_ROLE) == PRIMARY_ROLE]
    if not primary:
        problems.append({"field": "source_location", "reason": "no supporting excerpt location"})
    for loc in locs:
        role = loc.get("role") or PRIMARY_ROLE
        ok, why = locations.check_span(unit_texts.get(role), loc.get("char_start"), loc.get("char_end"),
                                       loc.get("excerpt"))
        if not ok:
            problems.append({"field": f"location:{role}", "reason": why, "claimed_literal": loc.get("excerpt"),
                             "char_start": loc.get("char_start"), "char_end": loc.get("char_end")})
    for fld in LITERAL_FIELDS:
        literal = content.get(fld)
        if literal in (None, ""):
            continue
        if fld not in spans:
            problems.append({"field": fld, "reason": "no source span recorded for this transcribed literal",
                             "claimed_literal": literal})
            continue
        role, s, e = _span_target(spans[fld])
        text = unit_texts.get(role)
        if text is None or s is None or e is None or s < 0 or e > len(text) or s >= e or text[s:e] != literal:
            problems.append({"field": fld, "reason": "claimed source string is absent from the specified span",
                             "claimed_literal": literal, "char_start": s, "char_end": e, "role": role})
            continue
        cut = span_cuts_token(text, s, e)
        if cut:
            problems.append({"field": fld, "reason": cut, "claimed_literal": literal, "char_start": s,
                             "char_end": e, "role": role})
    return problems


def span_cuts_token(text: str, start: int, end: int) -> str | None:
    """Reason when ``text[start:end]`` is only part of a longer number or word, else ``None``.

    "25" taken from "250", "8.5" from "18.5" or "m" from "min" are present as
    character strings but are not what the source states, so they do not count
    as grounded transcriptions.
    """
    first, last = text[start], text[end - 1]
    before = text[start - 1] if start > 0 else ""
    before2 = text[start - 2] if start > 1 else ""
    after = text[end] if end < len(text) else ""
    after2 = text[end + 1] if end + 1 < len(text) else ""
    if first.isdigit() and (before.isdigit() or (before in ".," and before2.isdigit())):
        return "the specified span starts inside a longer number in the source text"
    if last.isdigit() and (after.isdigit() or (after in ".," and after2.isdigit())):
        return "the specified span ends inside a longer number in the source text"
    if first.isalpha() and before.isalpha():
        return "the specified span starts inside a longer word in the source text"
    if last.isalpha() and after.isalpha():
        return "the specified span ends inside a longer word in the source text"
    return None


def stored_unit_texts(conn: sqlite3.Connection, locs: list[dict]) -> dict[str, str | None]:
    """Role -> stored text of the addressed unit, read from the database."""
    return {(l.get("role") or PRIMARY_ROLE): locations.unit_text(conn, l)[0] for l in locs}


def log_parse_failures(conn: sqlite3.Connection, context: str, loc: dict[str, Any] | None,
                       problems: list[dict[str, Any]], import_run_id: str | None = None) -> None:
    now = utc_now()
    loc = loc or {}
    conn.executemany(
        "INSERT INTO parse_failure (import_run_id, context, workbook_version_id, sheet, cell_ref, snapshot_id,"
        " unit_key, char_start, char_end, field, claimed_literal, reason, created_at) VALUES"
        " (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(import_run_id, context, loc.get("workbook_version_id"), loc.get("sheet"), loc.get("cell_ref"),
          loc.get("snapshot_id"), None if loc.get("unit_key") is None else str(loc.get("unit_key")),
          p.get("char_start"), p.get("char_end"), p.get("field"), p.get("claimed_literal"), p["reason"], now)
         for p in problems])


# ------------------------------------------------------------------ storage
@dataclass
class NewFinding:
    """A finding ready to be stored."""

    paper_id: str
    origin: str
    extraction_method: str
    content: dict[str, Any]
    locations: list[dict[str, Any]]
    spans: dict[str, Any] = field(default_factory=dict)
    stage_keys: list[str] = field(default_factory=list)
    stage_source: str = "imported"
    review_status: str = "unreviewed"
    review_flags: list[str] = field(default_factory=list)
    grounding_status: str = "grounded"
    source_scope: str = "workbook_summary"
    workbook_key: str | None = None
    match_key: str | None = None
    occurrence: int | None = None
    assignments: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("fnd"))
    location_resolves: dict[str, bool] = field(default_factory=dict)   # role -> resolves (default True)
    meta: dict[str, Any] = field(default_factory=dict)                 # caller's bookkeeping; not stored


_FINDING_COLUMNS = ("id", "paper_id", "study_id", "experiment_id", "comparison_group_id", "origin",
                    "extraction_method", "workbook_key", "match_key", "occurrence", "record_status",
                    "review_status", "grounding_status", "source_scope", "review_flags_json", "current_version",
                    "spans_json", "created_at", "updated_at", "created_by", *CONTENT_COLUMNS)
_FINDING_INSERT = (f"INSERT INTO finding ({', '.join(_FINDING_COLUMNS)}) "
                   f"VALUES ({', '.join('?' * len(_FINDING_COLUMNS))})")
_VERSION_INSERT = ("INSERT INTO finding_version (id, finding_id, version_no, change_kind, snapshot_json,"
                   " content_sha256, created_at, created_by, actor_kind, note) VALUES (?,?,?,?,?,?,?,?,?,?)")
_STAGE_INSERT = ("INSERT INTO finding_stage (finding_id, stage_id, assignment_source, status, assigned_by,"
                 " created_at, updated_at) VALUES (?,?,?,?,?,?,?)")


def insert_findings(conn: sqlite3.Connection, items: list[NewFinding], actor: Actor, change_kind: str,
                    stage_ids: dict[str, str], note: str | None = None) -> int:
    """Bulk-insert findings with version 1 snapshots, source locations and stage links."""
    if not items:
        return 0
    now = utc_now()
    f_rows, v_rows, l_rows, s_rows = [], [], [], []
    for it in items:
        if not it.locations:
            raise GroundingError("A finding cannot be stored without a source location.")
        a = it.assignments
        f_rows.append((it.id, it.paper_id, a.get("study_id"), a.get("experiment_id"), a.get("comparison_group_id"),
                       it.origin, it.extraction_method, it.workbook_key, it.match_key, it.occurrence, "active",
                       it.review_status, it.grounding_status, it.source_scope, canonical_json(it.review_flags), 1,
                       canonical_json(it.spans), now, now, actor.name, *_content_row(it.content)))
        snap = make_snapshot(it.content, it.stage_keys, it.spans, it.locations, it.review_flags, a,
                             it.grounding_status)
        v_rows.append((new_id("fnv"), it.id, 1, change_kind, canonical_json(snap),
                       content_hash(it.content, it.stage_keys), now, actor.name, actor.kind, note))
        for loc in it.locations:
            role = loc.get("role") or PRIMARY_ROLE
            l_rows.append(locations.row_tuple("finding", it.id, 1, loc, it.location_resolves.get(role, True),
                                              None, now))
        status = "suggested" if it.stage_source == "suggested" else "active"
        for key in it.stage_keys:
            s_rows.append((it.id, stage_ids[key], it.stage_source, status, actor.name, now, now))
    conn.executemany(_FINDING_INSERT, f_rows)
    conn.executemany(_VERSION_INSERT, v_rows)
    conn.executemany(locations.INSERT_SQL, l_rows)
    conn.executemany(_STAGE_INSERT, s_rows)
    return len(items)


def replace_locations(conn: sqlite3.Connection, finding_id: str, version_no: int, locs: list[dict[str, Any]],
                      resolves: dict[str, bool] | None = None, now: str | None = None) -> None:
    """Point a finding at new source locations; earlier locations are kept as non-current."""
    conn.execute("UPDATE source_location SET is_current = 0 WHERE owner_type = 'finding' AND owner_id = ?"
                 " AND is_current = 1", (finding_id,))
    conn.executemany(locations.INSERT_SQL, [
        locations.row_tuple("finding", finding_id, version_no, loc,
                            (resolves or {}).get(loc.get("role") or PRIMARY_ROLE, True), None, now)
        for loc in locs])


def set_stage_keys(conn: sqlite3.Connection, finding_id: str, stage_keys: Iterable[str], source: str,
                   stage_ids: dict[str, str], actor: Actor, now: str | None = None) -> None:
    """Make the active stage links of one assignment source equal to ``stage_keys`` (links are never deleted)."""
    now = now or utc_now()
    wanted = {stage_ids[k] for k in stage_keys}
    existing = {r["stage_id"]: r for r in conn.execute(
        "SELECT * FROM finding_stage WHERE finding_id = ? AND assignment_source = ?", (finding_id, source))}
    for sid, row in existing.items():
        target = "active" if sid in wanted else "removed"
        if row["status"] != target and not (target == "removed" and row["status"] in ("removed", "dismissed")):
            conn.execute("UPDATE finding_stage SET status = ?, updated_at = ?, assigned_by = ? WHERE id = ?",
                         (target, now, actor.name, row["id"]))
    for sid in wanted - set(existing):
        conn.execute(_STAGE_INSERT, (finding_id, sid, source, "active", actor.name, now, now))


def active_stage_keys(conn: sqlite3.Connection, finding_id: str) -> list[str]:
    return [r["key"] for r in conn.execute(
        "SELECT DISTINCT s.key FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id"
        " WHERE fs.finding_id = ? AND fs.status = 'active' ORDER BY s.position", (finding_id,))]


def mark_stale(conn: sqlite3.Connection, finding_id: str, reason: str, now: str | None = None) -> dict[str, int]:
    """Mark reviews and derived values that depend on a finding as stale.

    Called whenever the evidence behind a finding changes. Verification is never
    carried forward onto changed evidence.
    """
    now = now or utc_now()
    cur = conn.execute(
        "UPDATE extraction_review SET is_stale = 1, stale_reason = ?, stale_at = ? WHERE finding_id = ?"
        " AND is_stale = 0", (reason, now, finding_id))
    n_reviews = cur.rowcount
    cur = conn.execute(
        "UPDATE derived_value SET status = 'stale', stale_reason = ? WHERE status = 'current' AND id IN"
        " (SELECT derived_value_id FROM derived_value_input WHERE finding_id = ?)", (reason, finding_id))
    n_derived = cur.rowcount
    # a judgement of suitability for comparison was made on the earlier evidence; it is not carried forward
    cur = conn.execute("UPDATE finding SET comparison_eligibility = 'not_assessed' WHERE id = ? AND"
                       " comparison_eligibility != 'not_assessed'", (finding_id,))
    return {"reviews": n_reviews, "derived_values": n_derived, "comparison_eligibility_reset": cur.rowcount}


def write_new_version(conn: sqlite3.Connection, finding_id: str, *, content: dict[str, Any],
                      spans: dict[str, Any], locs: list[dict[str, Any]] | None, stage_keys: list[str],
                      actor: Actor, change_kind: str, review_status: str, review_flags: list[str],
                      grounding_status: str, note: str | None = None,
                      assignments: dict[str, Any] | None = None,
                      location_resolves: dict[str, bool] | None = None,
                      extra_columns: dict[str, Any] | None = None) -> int:
    """Store new content as the next version of a finding. Earlier versions stay untouched."""
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding {finding_id}")
    now = utc_now()
    version_no = row["current_version"] + 1
    assignments = assignments if assignments is not None else {k: row[k] for k in ASSIGNMENT_KEYS}
    sets = [f"{c} = ?" for c in CONTENT_COLUMNS] + [
        "spans_json = ?", "review_status = ?", "review_flags_json = ?", "grounding_status = ?",
        "current_version = ?", "updated_at = ?", "study_id = ?", "experiment_id = ?", "comparison_group_id = ?"]
    params = [*_content_row(content), canonical_json(spans), review_status, canonical_json(review_flags),
              grounding_status, version_no, now, assignments.get("study_id"), assignments.get("experiment_id"),
              assignments.get("comparison_group_id")]
    for col, val in (extra_columns or {}).items():
        sets.append(f"{col} = ?")
        params.append(val)
    conn.execute(f"UPDATE finding SET {', '.join(sets)} WHERE id = ?", (*params, finding_id))
    if locs is not None:
        replace_locations(conn, finding_id, version_no, locs, location_resolves, now)
        snap_locs = locs
    else:
        snap_locs = locations.current_locations(conn, "finding", finding_id)
    snap = make_snapshot(content, stage_keys, spans, snap_locs, review_flags, assignments, grounding_status)
    conn.execute(_VERSION_INSERT, (new_id("fnv"), finding_id, version_no, change_kind, canonical_json(snap),
                                   content_hash(content, stage_keys), now, actor.name, actor.kind, note))
    return version_no


# ------------------------------------------------------------------ reading
def get_finding(conn: sqlite3.Connection, finding_id: str) -> dict[str, Any]:
    """A finding with parsed JSON fields, stages, current source locations and its paper citation."""
    row = conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown finding {finding_id}")
    out = dict(row)
    out["content"] = content_from_row(row)
    out["review_flags"] = loads(row["review_flags_json"], [])
    out["spans"] = loads(row["spans_json"], {})
    out["stages"] = [dict(r) for r in conn.execute(
        "SELECT s.key, s.name, fs.assignment_source, fs.status FROM finding_stage fs JOIN stage s"
        " ON s.id = fs.stage_id WHERE fs.finding_id = ? ORDER BY s.position, fs.assignment_source", (finding_id,))]
    out["locations"] = locations.current_locations(conn, "finding", finding_id)
    paper = conn.execute("SELECT id, title, citation_text, year, doi, resolution_status, identity_status"
                         " FROM paper WHERE id = ?", (row["paper_id"],)).fetchone()
    out["paper"] = dict(paper) if paper else None
    return out


def versions(conn: sqlite3.Connection, finding_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM finding_version WHERE finding_id = ? ORDER BY version_no", (finding_id,))
    return [{**dict(r), "snapshot": loads(r["snapshot_json"], {})} for r in rows]


def reviews(conn: sqlite3.Connection, finding_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM extraction_review WHERE finding_id = ? ORDER BY created_at, id",
                        (finding_id,))
    return [{**dict(r), "checks": loads(r["checks_json"], {})} for r in rows]


def stage_id_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["key"]: r["id"] for r in conn.execute("SELECT key, id FROM stage")}


def record_audit_for_version(conn: sqlite3.Connection, actor: Actor, action: str, finding_id: str,
                             details: dict[str, Any]) -> None:
    audit.record(conn, actor, action, "finding", finding_id, details)


#: Parser/import flags that mean an extraction is ambiguous and is kept as a draft until a reviewer resolves it.
DRAFT_FLAGS = frozenset({
    "multiple_quantities_in_segment", "timepoint_assignment_ambiguous", "sample_size_assignment_ambiguous",
    "range_bounds_not_ascending", "from_to_may_be_change_not_range", "transcription_rejected", "unit_ambiguous",
    "mixed_units_in_expression", "ambiguous_identity_match", "uncertainty_type_ambiguous",
    "proposed_by_external_proposer",
})
UNSUPPORTED_FLAG = "grounding_unsupported"


def last_import_hash(conn: sqlite3.Connection, finding_id: str) -> str | None:
    """Content hash of the most recent version that came from a workbook import (not from a reviewer)."""
    row = conn.execute(
        "SELECT content_sha256 FROM finding_version WHERE finding_id = ? AND change_kind IN"
        " ('imported','reimport_modified') ORDER BY version_no DESC LIMIT 1", (finding_id,)).fetchone()
    return row["content_sha256"] if row else None
