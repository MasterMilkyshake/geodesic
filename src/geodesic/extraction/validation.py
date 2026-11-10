"""Rule-based validation of stored findings.

A validation run re-checks every active finding against the stored source text
and against a fixed list of consistency rules, and stores what it found
(``validation_run`` / ``validation_issue``). Runs are append-only: a later run
does not erase an earlier one, so the record shows what was known when.

Validation checks *traceability and internal consistency*. It cannot tell
whether a finding represents its source correctly - that is the reviewer's
judgement - and a run with no issues does not make findings "verified".

Rules (``RULES_VERSION`` identifies this list)
----------------------------------------------
error
    ``location_missing``            no supporting-excerpt location
    ``location_unresolved``         stored text at the location differs from the recorded excerpt
    ``literal_not_at_span``         a transcribed literal is not the stored text at its span, or is only part of
                                    a longer number or word there
    ``grounding_status_inconsistent`` finding is marked grounded but a grounding check fails
    ``value_literal_mismatch``      a numeric field holds a number that is not written in its literal
    ``verified_without_current_review`` status 'verified' without a non-stale verification of the current evidence
    ``source_document_removed``     the cited document was removed from the project
warning
    ``unsupported``                 finding is marked unsupported (cannot be verified until re-grounded)
    ``range_not_ascending``         range whose lower end exceeds its upper end
    ``unit_unrecognized``           unit is not in the unit table or project configuration
    ``unit_ambiguous``              unit symbol has more than one common reading
    ``relative_without_reference``  relative quantity without a recorded reference definition
    ``derived_value_stale``         a computed value that used this finding is stale
    ``assignment_inconsistent``     study / experiment / comparison-group assignment does not fit together
info
    ``unit_state_not_recorded``     measurement without unit and without a recorded reason
    ``field_state_not_recorded``    empty key field without a missing-information status
    ``uncertainty_type_unspecified`` "±" whose meaning (SD, SE, CI ...) the source does not state
    ``open_ambiguity_flags``        parser ambiguity flags not yet resolved by a reviewer
    ``unparsed_content``            content the parser could not read
    ``no_stage``                    not assigned to any stage
    ``paper_unresolved``            the cited paper could not be resolved to a record with an identifier
    ``superseded_snapshot``         cites a document snapshot that is no longer the latest parse
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from .. import audit, findings as F, locations
from ..db import transaction
from ..errors import NotFoundError
from ..project import Project
from ..quantities import units
from ..util import Actor, loads, new_id, utc_now
from .manual import number_problems

RULES_VERSION = "1.0"
CHUNK = 5000
SEVERITY = {
    "location_missing": "error", "location_unresolved": "error", "literal_not_at_span": "error",
    "grounding_status_inconsistent": "error", "value_literal_mismatch": "error",
    "verified_without_current_review": "error", "source_document_removed": "error",
    "unsupported": "warning", "range_not_ascending": "warning", "unit_unrecognized": "warning",
    "unit_ambiguous": "warning", "relative_without_reference": "warning", "derived_value_stale": "warning",
    "assignment_inconsistent": "warning",
    "unit_state_not_recorded": "info", "field_state_not_recorded": "info", "uncertainty_type_unspecified": "info",
    "open_ambiguity_flags": "info", "unparsed_content": "info", "no_stage": "info", "paper_unresolved": "info",
    "superseded_snapshot": "info",
}
#: status key -> content field that must be filled or carry a missing-information status (measurements)
_KEY_FIELDS = (("outcome", "outcome"), ("timepoint", "timepoint_literal"), ("sample_size", "sample_size"),
               ("uncertainty", "uncertainty_type"))
_LOC_COLUMNS = ("owner_id, role, kind, workbook_version_id, sheet, cell_ref, snapshot_id, unit_key, table_key,"
                " row_idx, col_idx, char_start, char_end, excerpt")


class _TextCache:
    """Stored unit texts, read once per unit (many findings share a cell or a page)."""

    def __init__(self, conn: sqlite3.Connection, limit: int = 4000) -> None:
        self.conn, self.limit, self.data = conn, limit, {}

    def get(self, loc: dict[str, Any]) -> str | None:
        key = (loc["kind"], loc.get("workbook_version_id"), loc.get("sheet"), loc.get("cell_ref"),
               loc.get("snapshot_id"), loc.get("unit_key"), loc.get("table_key"), loc.get("row_idx"),
               loc.get("col_idx"))
        if key not in self.data:
            if len(self.data) >= self.limit:
                self.data.clear()
            self.data[key] = locations.unit_text(self.conn, loc)[0]
        return self.data[key]


def _check(f: sqlite3.Row, locs: list[dict[str, Any]], texts: _TextCache, extra_units: dict[str, str],
           ctx: dict[str, Any]) -> Iterator[tuple[str, str | None, str]]:
    """Issues of one finding as ``(code, field, message)``."""
    content = F.content_from_row(f)
    spans = loads(f["spans_json"], {})
    flags = loads(f["review_flags_json"], [])
    status = content.get("field_status") or {}
    unit_texts = {(l.get("role") or F.PRIMARY_ROLE): texts.get(l) for l in locs}
    problems = F.grounding_problems(content, spans, unit_texts, locs)
    for p in problems:
        if p["field"] == "source_location":
            yield "location_missing", None, "The finding has no supporting-excerpt location."
        elif p["field"].startswith("location:"):
            yield "location_unresolved", p["field"], f"{p['field']}: {p['reason']}"
        else:
            yield "literal_not_at_span", p["field"], f"{p['field']} {p.get('claimed_literal')!r}: {p['reason']}"
    if f["grounding_status"] == "grounded" and problems:
        yield ("grounding_status_inconsistent", None,
               "The finding is marked grounded but its source no longer resolves; re-ground or re-import it.")
    if f["grounding_status"] != "grounded":
        yield "unsupported", None, "The finding is marked unsupported and cannot be verified until it is re-grounded."
    for p in number_problems(content):
        yield "value_literal_mismatch", p["field"], p["reason"]
    if content["statement_type"] == "range" and content["value_low"] is not None and \
            content["value_high"] is not None and content["value_low"] > content["value_high"]:
        yield "range_not_ascending", "value_literal", (
            f"Range {content['value_literal']!r} has a lower end above its upper end "
            "(it may describe a change rather than a range).")
    has_value = content["statement_type"] in ("exact", "approximate", "range", "bound")
    if has_value:
        ustatus, _ = units.lookup(content["unit_original"], extra_units)
        if ustatus == "missing" and "unit_original" not in status:
            yield "unit_state_not_recorded", "unit_original", (
                "The value has no unit and no recorded reason (not reported / not applicable / not yet extracted).")
        elif ustatus == "unrecognized":
            yield "unit_unrecognized", "unit_original", (
                f"Unit {content['unit_original']!r} is not in the unit table or the project's extra units; "
                "it is kept as written and never converted.")
        elif ustatus == "ambiguous":
            yield "unit_ambiguous", "unit_original", (
                f"Unit {content['unit_original']!r} has more than one common reading; it is not converted.")
        if content["relative_semantics"] not in (None, "absolute") and not content["reference_definition"] \
                and status.get("reference_definition") not in ("not_reported", "not_applicable"):
            yield "relative_without_reference", "reference_definition", (
                f"Relative quantity ({content['relative_semantics']}) without a recorded reference definition; "
                "it cannot be compared with other values.")
    if content["uncertainty_type"] == "unspecified_plus_minus":
        yield "uncertainty_type_unspecified", "uncertainty_type", (
            "The source gives '±' without saying what it is (SD, SE, CI ...).")
    if content["kind"] == "measurement":
        empty = [key for key, fld in _KEY_FIELDS if content.get(fld) in (None, "") and key not in status]
        if empty:
            yield "field_state_not_recorded", ",".join(empty), (
                f"Empty without a missing-information status: {', '.join(empty)}.")
    open_flags = sorted(set(flags) & F.DRAFT_FLAGS)
    if open_flags:
        yield "open_ambiguity_flags", None, f"Unresolved parser flags: {', '.join(open_flags)}."
    if content["statement_type"] == "unparsed":
        yield "unparsed_content", None, "The parser could not read this content; it needs manual extraction."
    if f["review_status"] == "verified" and f["id"] not in ctx["verified_ok"]:
        yield "verified_without_current_review", None, (
            f"Marked verified, but no current (non-stale) verification of the evidence in version "
            f"{f['current_version']} exists.")
    if f["id"] in ctx["stale_derived"]:
        yield "derived_value_stale", None, (
            f"{ctx['stale_derived'][f['id']]} computed value(s) that used this finding are stale; recompute them.")
    if f["id"] not in ctx["staged"]:
        yield "no_stage", None, "The finding is not assigned to any stage."
    if f["paper_id"] in ctx["unresolved_papers"]:
        yield "paper_unresolved", None, "The cited paper is not resolved to a record with a valid identifier."
    for l in locs:
        sid = l.get("snapshot_id")
        if sid and sid in ctx["removed_snapshots"]:
            yield "source_document_removed", f"location:{l['role']}", (
                "The cited document was removed from the project; the stored text remains, the file is not in use.")
        elif sid and sid in ctx["old_snapshots"]:
            yield "superseded_snapshot", f"location:{l['role']}", (
                "The cited snapshot is not the document's latest parse (the citation itself still resolves).")
    exp, grp, study = f["experiment_id"], f["comparison_group_id"], f["study_id"]
    if grp and ctx["group_experiment"].get(grp) != exp:
        yield "assignment_inconsistent", "comparison_group_id", "The comparison group belongs to another experiment."
    if exp and ctx["experiment_study"].get(exp) not in (None, study):
        yield "assignment_inconsistent", "experiment_id", "The experiment belongs to another study."


def _context(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        # A verification covers the current version when it was made on it, or on an earlier version whose
        # evidence (content, literal spans, source locations) is identical - an assignment to a study or
        # experiment creates a new version without changing the evidence that was verified.
        "verified_ok": {r[0] for r in conn.execute(
            "SELECT f.id FROM finding f JOIN extraction_review r ON r.finding_id = f.id AND r.action = 'verify'"
            " AND r.is_stale = 0 AND r.finding_version_no = f.current_version WHERE f.review_status = 'verified'"
            " UNION "
            "SELECT f.id FROM finding f JOIN extraction_review r ON r.finding_id = f.id AND r.action = 'verify'"
            " AND r.is_stale = 0 AND r.finding_version_no < f.current_version"
            " JOIN finding_version vr ON vr.finding_id = f.id AND vr.version_no = r.finding_version_no"
            " JOIN finding_version vc ON vc.finding_id = f.id AND vc.version_no = f.current_version"
            " WHERE f.review_status = 'verified'"
            " AND json_extract(vr.snapshot_json, '$.content') = json_extract(vc.snapshot_json, '$.content')"
            " AND json_extract(vr.snapshot_json, '$.spans') = json_extract(vc.snapshot_json, '$.spans')"
            " AND json_extract(vr.snapshot_json, '$.locations') = json_extract(vc.snapshot_json, '$.locations')")},
        "stale_derived": {r[0]: r[1] for r in conn.execute(
            "SELECT di.finding_id, COUNT(*) FROM derived_value_input di JOIN derived_value d"
            " ON d.id = di.derived_value_id WHERE d.status = 'stale' GROUP BY di.finding_id")},
        "staged": {r[0] for r in conn.execute("SELECT DISTINCT finding_id FROM finding_stage WHERE status = 'active'")},
        "unresolved_papers": {r[0] for r in conn.execute("SELECT id FROM paper WHERE resolution_status != 'resolved'")},
        "removed_snapshots": {r[0] for r in conn.execute(
            "SELECT s.id FROM document_snapshot s JOIN document d ON d.id = s.document_id"
            " WHERE d.record_status = 'removed'")},
        "old_snapshots": {r[0] for r in conn.execute(
            "SELECT s.id FROM document_snapshot s WHERE s.snapshot_no < (SELECT MAX(x.snapshot_no) FROM"
            " document_snapshot x WHERE x.document_id = s.document_id)")},
        "group_experiment": {r[0]: r[1] for r in conn.execute("SELECT id, experiment_id FROM comparison_group")},
        "experiment_study": {r[0]: r[1] for r in conn.execute("SELECT id, study_id FROM experiment")},
    }


def check_findings(project: Project, finding_ids: list[str] | None = None) -> Iterator[tuple[str, str, str | None, str]]:
    """Yield ``(finding_id, code, field, message)`` for active findings without storing anything."""
    conn = project.conn
    ctx = _context(conn)
    texts = _TextCache(conn)
    extra_units = project.config.parser.extra_units
    if finding_ids is not None:
        batches: list[tuple[str, list[Any]]] = [
            (f"id IN ({','.join('?' * len(finding_ids[i:i + 500]))})", list(finding_ids[i:i + 500]))
            for i in range(0, len(finding_ids), 500)]
    else:
        lo, hi = conn.execute("SELECT COALESCE(MIN(pk), 0), COALESCE(MAX(pk), -1) FROM finding").fetchone()
        batches = [("pk BETWEEN ? AND ?", [a, min(a + CHUNK - 1, hi)]) for a in range(lo, hi + 1, CHUNK)]
    for where, params in batches:
        rows = conn.execute(f"SELECT * FROM finding WHERE record_status = 'active' AND {where} ORDER BY pk",  # noqa: S608
                            params).fetchall()
        if not rows:
            continue
        locs: dict[str, list[dict[str, Any]]] = {}
        for l in conn.execute(
                f"SELECT {_LOC_COLUMNS} FROM source_location WHERE owner_type = 'finding' AND is_current = 1 AND"  # noqa: S608
                f" owner_id IN (SELECT id FROM finding WHERE record_status = 'active' AND {where})", params):
            locs.setdefault(l["owner_id"], []).append(dict(l))
        for f in rows:
            for code, fld, message in _check(f, locs.get(f["id"], []), texts, extra_units, ctx):
                yield f["id"], code, fld, message


def run_validation(project: Project, actor: Actor) -> dict[str, Any]:
    """Validate every active finding and store the result as a new run. Returns the run summary."""
    conn = project.conn
    run_id, now = new_id("val"), utc_now()
    n_findings = conn.execute("SELECT COUNT(*) FROM finding WHERE record_status = 'active'").fetchone()[0]
    issues = [(run_id, fid, code, SEVERITY[code], fld, message)
              for fid, code, fld, message in check_findings(project)]
    with transaction(conn):
        conn.execute("INSERT INTO validation_run (id, created_at, actor, n_findings, n_issues, rules_version)"
                     " VALUES (?,?,?,?,?,?)", (run_id, now, actor.name, n_findings, len(issues), RULES_VERSION))
        conn.executemany("INSERT INTO validation_issue (run_id, finding_id, code, severity, field, message)"
                         " VALUES (?,?,?,?,?,?)", issues)
        out = summary(conn, run_id)
        audit.record(conn, actor, "validation.run", "validation_run", run_id, {
            "rules_version": RULES_VERSION, "n_findings": n_findings, "n_issues": len(issues),
            "by_severity": out["by_severity"], "by_code": out["by_code"]})
    return out


# ------------------------------------------------------------------ reading
def latest_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM validation_run ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def summary(conn: sqlite3.Connection, run_id: str | None = None) -> dict[str, Any]:
    """Counts of a run (default: the latest) by severity and by rule."""
    run = conn.execute("SELECT * FROM validation_run WHERE id = ?", (run_id,)).fetchone() if run_id else None
    if run_id and run is None:
        raise NotFoundError(f"Unknown validation run {run_id}")
    run = dict(run) if run else latest_run(conn)
    if run is None:
        return {"run_id": None, "statement": "No validation run has been made in this project."}
    by_code = {r["code"]: r["n"] for r in conn.execute(
        "SELECT code, COUNT(*) n FROM validation_issue WHERE run_id = ? GROUP BY code ORDER BY code", (run["id"],))}
    by_sev = {"error": 0, "warning": 0, "info": 0}
    for code, n in by_code.items():
        by_sev[SEVERITY.get(code, "info")] += n
    affected = conn.execute("SELECT COUNT(DISTINCT finding_id) FROM validation_issue WHERE run_id = ?",
                            (run["id"],)).fetchone()[0]
    with_errors = conn.execute("SELECT COUNT(DISTINCT finding_id) FROM validation_issue WHERE run_id = ? AND"
                               " severity = 'error'", (run["id"],)).fetchone()[0]
    return {"run_id": run["id"], "created_at": run["created_at"], "actor": run["actor"],
            "rules_version": run["rules_version"], "n_findings": run["n_findings"], "n_issues": run["n_issues"],
            "findings_with_issues": affected, "findings_with_errors": with_errors, "by_severity": by_sev,
            "by_code": by_code,
            "statement": "Validation checks traceability and internal consistency only; it does not verify that "
                         "findings represent their sources correctly."}


def issues(conn: sqlite3.Connection, run_id: str | None = None, *, code: str | None = None,
           severity: str | None = None, finding_id: str | None = None, limit: int | None = 200,
           offset: int = 0) -> list[dict[str, Any]]:
    """Issues of a run (default: the latest), most severe first."""
    if run_id is None:
        run = latest_run(conn)
        if run is None:
            return []
        run_id = run["id"]
    sql, params = ("SELECT i.*, f.paper_id, f.kind, f.review_status FROM validation_issue i JOIN finding f"
                   " ON f.id = i.finding_id WHERE i.run_id = ?"), [run_id]
    for col, val in (("i.code", code), ("i.severity", severity), ("i.finding_id", finding_id)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    sql += " ORDER BY CASE i.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, i.code, i.id"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    return [dict(r) for r in conn.execute(sql, params)]
