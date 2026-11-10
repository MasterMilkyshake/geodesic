"""Further export tables: documents, searches, screening, hierarchy, derived values, validation, comparisons.

Kept apart from :mod:`geodesic.export.tables` only to keep that module readable;
:func:`geodesic.export.tables.standard_tables` returns both groups. Search-run
client identification (which may contain a contact address) and raw provider
responses are deliberately not exported.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..util import loads
from .tables import Table, _join


def _rows(conn: sqlite3.Connection, cols: list[str], sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [{c: r[c] for c in cols} for r in conn.execute(sql, params)]


def documents_table(conn: sqlite3.Connection) -> Table:
    cols = ["document_id", "paper_id", "role", "parent_document_id", "format", "original_filename", "sha256",
            "size_bytes", "source", "source_url", "license", "content_scope", "declared_data_class", "retrieved_at",
            "added_by", "record_status", "snapshot_no", "parser", "parser_version", "parse_status", "failure_reason",
            "text_units", "characters", "tables", "ocr_applied"]
    rows = _rows(conn, cols, """
        SELECT d.id AS document_id, d.paper_id, d.role, d.parent_document_id, d.format, d.original_filename, d.sha256,
               d.size_bytes, d.source, d.source_url, d.license, d.content_scope, d.declared_data_class,
               d.retrieved_at, d.added_by, d.record_status, s.snapshot_no, s.parser, s.parser_version,
               s.status AS parse_status, s.failure_reason, s.n_units AS text_units, s.n_chars AS characters,
               s.n_tables AS tables, s.ocr_applied
        FROM document d LEFT JOIN document_snapshot s ON s.document_id = d.id AND s.snapshot_no =
             (SELECT MAX(x.snapshot_no) FROM document_snapshot x WHERE x.document_id = d.id)
        ORDER BY d.retrieved_at, d.id""")
    return Table("documents", "Documents", cols, rows,
                 "One row per stored document with its latest parse snapshot. sha256 is the hash of the preserved "
                 "file; the file itself is not exported. 'license' is the licence as supplied by the source, empty "
                 "when none was supplied. parse_status 'image_only' means no text layer was found (no OCR is done).",
                 links={"paper_id": "papers"})


def search_runs_table(conn: sqlite3.Connection) -> Table:
    cols = ["search_run_id", "provider", "interface", "mode", "executed_query", "filters", "started_at",
            "finished_at", "page_size", "max_records", "pages_retrieved", "retrieved_count", "provider_total",
            "status", "completeness_note", "protocol_version", "protocol_status_at_run", "exploratory",
            "source_file_name", "source_file_sha256", "declared_data_class", "actor"]
    rows = _rows(conn, cols, """
        SELECT r.id AS search_run_id, r.provider, r.interface, r.mode, r.executed_query, r.filters_json AS filters,
               r.started_at, r.finished_at, r.page_size, r.max_records, r.pages_retrieved, r.retrieved_count,
               r.provider_total, r.status, r.completeness_note, v.version AS protocol_version,
               r.protocol_status_at_run, r.exploratory, r.source_file_name, r.source_file_sha256,
               r.declared_data_class, r.actor
        FROM search_run r LEFT JOIN protocol_version v ON v.id = r.protocol_version_id ORDER BY r.started_at, r.id""")
    return Table("search_runs", "Search runs", cols, rows,
                 "One row per search run or search-file import. mode 'fixture' marks bundled synthetic responses, "
                 "'live' a provider response. A run describes what one interface returned on one date; it is not a "
                 "statement that the search was exhaustive. 'exploratory' runs were made under a provisional protocol.")


def screening_table(conn: sqlite3.Connection) -> Table:
    cols = ["decision_id", "paper_id", "stage", "decision", "reason_kind", "criterion_id", "reason_text", "reviewer",
            "decider_kind", "decision_role", "finality", "decided_at", "protocol_version",
            "protocol_status_at_decision", "supporting_text", "is_current", "supersedes_id", "needs_rereview",
            "rereview_reason"]
    rows = _rows(conn, cols, """
        SELECT d.id AS decision_id, d.paper_id, d.stage, d.decision, d.reason_kind, d.criterion_id, d.reason_text,
               d.reviewer, d.decider_kind, d.decision_role, d.finality, d.decided_at, v.version AS protocol_version,
               d.protocol_status_at_decision, d.supporting_text, d.is_current, d.supersedes_id, d.needs_rereview,
               d.rereview_reason
        FROM screening_decision d JOIN protocol_version v ON v.id = d.protocol_version_id
        ORDER BY d.decided_at, d.id""")
    return Table("screening_decisions", "Screening decisions (full history)", cols, rows,
                 "Every screening decision ever recorded, including superseded ones (is_current = 0). decider_kind "
                 "separates human, automated and simulated decisions; finality 'provisional' decisions were made "
                 "under an unconfirmed protocol and are drafts.", links={"paper_id": "papers"})


def assessments_table(conn: sqlite3.Connection) -> Table:
    cols = ["assessment_id", "paper_id", "dimension", "value", "rationale", "reviewer", "decider_kind",
            "protocol_version", "created_at", "is_current"]
    rows = _rows(conn, cols, """
        SELECT a.id AS assessment_id, a.paper_id, a.dimension, a.value, a.rationale, a.reviewer, a.decider_kind,
               v.version AS protocol_version, a.created_at, a.is_current
        FROM paper_assessment a LEFT JOIN protocol_version v ON v.id = a.protocol_version_id
        ORDER BY a.created_at, a.id""")
    return Table("paper_assessments", "Reviewer assessments of papers", cols, rows,
                 "Relevance to the primary question, suitability for quantitative comparison and suitability for "
                 "modelling are recorded separately from eligibility and from each other. Each row is one reviewer's "
                 "judgement.", links={"paper_id": "papers"})


def studies_table(conn: sqlite3.Connection) -> Table:
    cols = ["study_id", "label", "description", "design", "reports", "record_status", "created_at", "created_by"]
    reports: dict[str, list[str]] = {}
    for r in conn.execute("SELECT study_id, paper_id FROM study_report WHERE active = 1 ORDER BY created_at, paper_id"):
        reports.setdefault(r[0], []).append(r[1])
    rows = []
    for r in conn.execute("SELECT * FROM study ORDER BY created_at, id"):
        rows.append({"study_id": r["id"], "label": r["label"], "description": r["description"], "design": r["design"],
                     "reports": _join(reports.get(r["id"], [])), "record_status": r["record_status"],
                     "created_at": r["created_at"], "created_by": r["created_by"]})
    return Table("studies", "Studies", cols, rows,
                 "A study groups the reports (papers) a reviewer identified as describing the same investigation. "
                 "The hierarchy is sparse: papers without a study row were simply not grouped.")


def experiments_table(conn: sqlite3.Connection) -> Table:
    cols = ["experiment_id", "paper_id", "study_id", "label", "description", "system_context", "design",
            "comparison_groups", "findings", "record_status", "created_at", "created_by"]
    groups: dict[str, list[str]] = {}
    for r in conn.execute("SELECT experiment_id, label, role FROM comparison_group WHERE record_status = 'active'"
                          " ORDER BY created_at, id"):
        groups.setdefault(r[0], []).append(r["label"] + (f" ({r['role']})" if r["role"] else ""))
    n = {r[0]: r[1] for r in conn.execute("SELECT experiment_id, COUNT(*) FROM finding WHERE experiment_id IS NOT NULL"
                                          " AND record_status = 'active' GROUP BY experiment_id")}
    rows = []
    for r in conn.execute("SELECT * FROM experiment ORDER BY created_at, id"):
        rows.append({"experiment_id": r["id"], "paper_id": r["paper_id"], "study_id": r["study_id"],
                     "label": r["label"], "description": r["description"], "system_context": r["system_context"],
                     "design": r["design"], "comparison_groups": _join(groups.get(r["id"], [])),
                     "findings": n.get(r["id"], 0), "record_status": r["record_status"],
                     "created_at": r["created_at"], "created_by": r["created_by"]})
    return Table("experiments", "Experiments", cols, rows,
                 "Findings assigned to one experiment are treated as one unit when independent units are counted.",
                 links={"paper_id": "papers"})


def derived_table(conn: sqlite3.Connection) -> Table:
    cols = ["derived_value_id", "kind", "rule_id", "formula", "input_findings", "inputs", "output_value",
            "output_low", "output_high", "output_unit", "carried", "status", "stale_reason", "created_at",
            "created_by"]
    rows = []
    for r in conn.execute("SELECT * FROM derived_value ORDER BY created_at, id"):
        inputs = loads(r["inputs_json"], [])
        rows.append({
            "derived_value_id": r["id"], "kind": r["kind"], "rule_id": r["rule_id"], "formula": r["formula"],
            "input_findings": _join(i.get("finding_id") for i in inputs),
            "inputs": _join(f"{i.get('finding_id')} v{i.get('version_no')} {i.get('field')}="
                            f"{i.get('source_literal')!r} -> {i.get('value')} {i.get('unit') or ''}".strip()
                            for i in inputs),
            "output_value": r["output_value"], "output_low": r["output_low"], "output_high": r["output_high"],
            "output_unit": r["output_unit"], "carried": r["carries_json"], "status": r["status"],
            "stale_reason": r["stale_reason"], "created_at": r["created_at"], "created_by": r["created_by"]})
    return Table("derived_values", "Normalized and derived values", cols, rows,
                 "Values computed by the application from findings, each with its rule, formula and the exact inputs "
                 "(finding, version, literal source string). They are stored apart from findings and never replace "
                 "what a source states. status 'stale' means an input changed after the value was computed.")


def vocabulary_table(conn: sqlite3.Connection) -> Table:
    cols = ["field", "original_term", "normalized_term", "note", "active", "created_at", "created_by"]
    rows = _rows(conn, cols, "SELECT * FROM vocabulary_term ORDER BY field, original_term")
    return Table("vocabulary_terms", "Reviewer term mappings", cols, rows,
                 "Original terms stay as stored on every finding. A normalized term is a reviewer's mapping used "
                 "only for grouping; terms are never merged without such a mapping.")


def validation_table(conn: sqlite3.Connection) -> Table:
    cols = ["run_id", "run_at", "finding_id", "severity", "code", "field", "message"]
    run = conn.execute("SELECT * FROM validation_run ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    rows: list[dict[str, Any]] = []
    note = ("Issues of the latest validation run. Validation checks traceability and internal consistency only; "
            "it does not verify that findings represent their sources correctly.")
    if run is None:
        note = "No validation run has been made in this project. " + note
    else:
        note += f" Run {run['id']} at {run['created_at']} checked {run['n_findings']} finding(s)."
        for r in conn.execute("SELECT * FROM validation_issue WHERE run_id = ? ORDER BY CASE severity WHEN 'error'"
                              " THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, code, id", (run["id"],)):
            rows.append({"run_id": run["id"], "run_at": run["created_at"], "finding_id": r["finding_id"],
                         "severity": r["severity"], "code": r["code"], "field": r["field"], "message": r["message"]})
    return Table("validation_issues", "Validation issues (latest run)", cols, rows, note,
                 links={"finding_id": "findings"})


def comparison_sets_table(conn: sqlite3.Connection) -> Table:
    from .. import comparison   # comparison imports this package; resolved at call time

    cols = ["comparison_set_id", "name", "description", "classification", "assessed_classification", "blocking",
            "cautions", "findings", "independent_units_at_most", "finding_id", "is_current",
            "changed_since_assessment", "created_at", "created_by"]
    rows = []
    for s in comparison.list_sets(conn):
        a = s["assessment"]
        common = {
            "comparison_set_id": s["id"], "name": s["name"], "description": s["description"],
            "classification": s["classification"], "assessed_classification": a.get("classification"),
            "blocking": _join(a.get("blocking", [])), "cautions": _join(a.get("cautions", [])),
            "findings": len(s["finding_ids"]),
            "independent_units_at_most": (a.get("independence") or {}).get("independent_units_at_most"),
            "is_current": s["is_current"], "created_at": s["created_at"], "created_by": s["created_by"]}
        for fid in s["finding_ids"]:
            rows.append({**common, "finding_id": fid, "changed_since_assessment": fid in s["changed_since_assessment"]})
    return Table("comparison_sets", "Saved comparison sets", cols, rows,
                 "One row per finding of each saved comparison set. A set records a reviewer's compatibility "
                 "assessment at one moment; is_current is false when a finding changed afterwards. "
                 "'quantitative_candidate' means the recorded fields do not rule out a quantitative comparison; "
                 "nothing is pooled, averaged or ranked.", links={"finding_id": "findings"})


def workflow_tables(conn: sqlite3.Connection) -> list[Table]:
    return [documents_table(conn), search_runs_table(conn), screening_table(conn), assessments_table(conn),
            studies_table(conn), experiments_table(conn), derived_table(conn), vocabulary_table(conn),
            validation_table(conn), comparison_sets_table(conn)]
