"""Evidence tables as plain data.

Every output format (Excel, CSV, JSON, HTML report, dashboard) is rendered from
the tables built here, so the same numbers and wording appear everywhere.
Excerpts are short quotations; whole documents are never exported.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import locations, papers, protocol, query
from ..project import Project
from ..util import loads


@dataclass
class Table:
    """A named table: ordered columns and rows (dicts keyed by column)."""

    name: str                     # short machine name, also the Excel sheet / CSV file stem
    title: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""                # what the table is and how to read it
    links: dict[str, str] = field(default_factory=dict)   # column -> table whose first column it references


def _join(values) -> str:
    return "; ".join(str(v) for v in values if v not in (None, ""))


def location_text(loc: dict[str, Any]) -> str:
    """Address of a source location, naming the workbook/document version rather than only internal IDs."""
    span = f" chars {loc['char_start']}-{loc['char_end']}" if loc.get("char_start") is not None else ""
    kind = loc.get("kind")
    if kind == "workbook_cell":
        ver = loc.get("workbook_version_no")
        wk = loc.get("loc_workbook_key") or "workbook"
        return f"{wk} v{ver} · {loc.get('sheet')}!{loc.get('cell_ref')}{span}"
    doc = f"document {loc.get('document_id')} snapshot {loc.get('snapshot_no')}"
    if kind == "pdf_page":
        return f"{doc} · page index {loc.get('unit_key')}{span}"
    if kind in ("xml_element", "html_element"):
        return f"{doc} · element {loc.get('unit_key')}{span}"
    if kind == "table_cell":
        return f"{doc} · table {loc.get('table_key')} row {loc.get('row_idx')} col {loc.get('col_idx')}{span}"
    return locations.describe(loc)


FINDING_COLUMNS = [
    "finding_id", "paper_id", "citation", "stages", "stage_assignment_sources", "kind", "statement_type",
    "source_wording", "system_context", "experimental_context", "intervention", "comparator", "outcome",
    "measurement_method", "observation_type", "value_literal", "value_num", "value_low", "value_high",
    "bound_type", "unit_original", "relative_semantics", "reference_definition", "timepoint_literal",
    "timepoint_value", "time_unit", "observation_kind", "summary_statistic", "sample_size_literal", "sample_size",
    "replicate_type", "uncertainty_type", "uncertainty_literal", "uncertainty_value", "uncertainty_low",
    "uncertainty_high", "confidence_level", "denominator", "normalization", "qualifiers", "interpretation_limits",
    "field_status", "extraction_method", "source_scope", "review_status", "grounding_status", "review_flags",
    "comparison_eligibility", "study_id", "experiment_id", "comparison_group_id", "source_location",
    "n_source_locations", "version_no", "record_status",
]


def finding_row(f: dict[str, Any]) -> dict[str, Any]:
    locs = f.get("locations") or []
    primary = next((l for l in locs if l["role"] == "supporting_excerpt"), None)
    row = {c: f.get(c) for c in FINDING_COLUMNS}
    row.update(
        finding_id=f["id"],
        citation=papers.citation_label({"authors": loads(f.get("paper_authors_json"), []), "year": f.get("paper_year"),
                                        "citation_text": f.get("paper_citation"), "title": f.get("paper_title"),
                                        "id": f["paper_id"]}),
        stages=_join(f.get("stages", [])),
        stage_assignment_sources=_join(f"{k}: {'+'.join(sorted(set(v)))}" for k, v in
                                       (f.get("stage_sources") or {}).items()),
        source_wording=locations.short_excerpt(f.get("source_wording")),
        qualifiers=_join(q.get("text") for q in f.get("qualifiers", [])),
        field_status=_join(f"{k}: {v}" for k, v in sorted((f.get("field_status") or {}).items())),
        review_flags=_join(f.get("review_flags", [])),
        source_location=location_text(primary) if primary else "",
        n_source_locations=len(locs),
        version_no=f.get("current_version"),
    )
    return row


def findings_table(conn: sqlite3.Connection, flt: query.FindingFilter | None = None) -> Table:
    rows = [finding_row(f) for f in query.iter_findings(conn, flt, with_locations=True)]
    return Table("findings", "Findings", FINDING_COLUMNS, rows,
                 "One row per finding. 'source_wording' is the text as written in the source; value_literal, "
                 "unit_original, timepoint_literal, sample_size_literal and uncertainty_literal are literal source "
                 "strings. Empty cells mean the field has no value; 'field_status' says why (not_reported, "
                 "not_applicable, not_yet_extracted, could_not_be_parsed). review_status 'verified' means a named "
                 "reviewer confirmed the finding against its source; everything else is unverified.",
                 links={"paper_id": "papers"})


LOCATION_COLUMNS = ["finding_id", "role", "kind", "location", "excerpt", "resolves", "resolution_note"]


def locations_table(conn: sqlite3.Connection, flt: query.FindingFilter | None = None) -> Table:
    rows = []
    for f in query.iter_findings(conn, flt, with_locations=True):
        for l in f["locations"]:
            rows.append({"finding_id": f["id"], "role": l["role"], "kind": l["kind"], "location": location_text(l),
                         "excerpt": locations.short_excerpt(l.get("excerpt")), "resolves": bool(l["resolves"]),
                         "resolution_note": l.get("resolution_note")})
    return Table("source_locations", "Source locations", LOCATION_COLUMNS, rows,
                 "Where each finding comes from. Character spans are 0-based, half-open offsets into the stored "
                 "text of the cell, page or element. 'resolves' is TRUE when the stored text at the location "
                 "equals the excerpt.", links={"finding_id": "findings"})


PAPER_COLUMNS = [
    "paper_id", "citation", "title", "authors", "year", "journal", "publication_type", "publication_status",
    "doi", "pmid", "pmcid", "urls", "resolution_status", "identity_status", "fulltext_availability",
    "correction_retraction", "stages", "n_findings", "n_documents", "screening_title_abstract",
    "screening_full_text", "origin", "citation_text", "open_metadata_conflicts", "record_status",
]


def _screen(p: dict[str, Any], stage: str) -> str:
    s = p["screening"].get(stage)
    if not s:
        return "not screened"
    text = s["outcome"]
    if s["finality"] == "provisional":
        text += " (provisional)"
    if s["needs_rereview"]:
        text += " [re-review needed]"
    return text


def papers_table(conn: sqlite3.Connection, flt: query.PaperFilter | None = None) -> Table:
    checks: dict[str, list[dict]] = {}
    for r in conn.execute("SELECT * FROM integrity_check"):
        checks.setdefault(r["paper_id"], []).append(dict(r))
    conflicts = {r[0]: r[1] for r in conn.execute(
        "SELECT paper_id, COUNT(*) FROM metadata_conflict WHERE status = 'open' GROUP BY paper_id")}
    rows = []
    for p in query.iter_papers(conn, flt):
        rows.append({
            "paper_id": p["id"], "citation": papers.citation_label(p), "title": p["title"],
            "authors": _join(p["authors"]), "year": p["year"], "journal": p["journal"],
            "publication_type": p["publication_type"], "publication_status": p["publication_status"],
            "doi": p["doi"], "pmid": p["pmid"], "pmcid": p["pmcid"], "urls": _join(p["urls"]),
            "resolution_status": p["resolution_status"], "identity_status": p["identity_status"],
            "fulltext_availability": p["fulltext_availability"],
            "correction_retraction": papers.integrity_statement(checks.get(p["id"], [])),
            "stages": _join(p["stages"]), "n_findings": p["n_findings"], "n_documents": p["n_documents"],
            "screening_title_abstract": _screen(p, "title_abstract"),
            "screening_full_text": _screen(p, "full_text"), "origin": p["origin"],
            "citation_text": locations.short_excerpt(p["citation_text"]),
            "open_metadata_conflicts": conflicts.get(p["id"], 0), "record_status": p["record_status"]})
    return Table("papers", "Papers (reports)", PAPER_COLUMNS, rows,
                 "One row per report. 'resolution_status' says whether the record carries a syntactically valid "
                 "persistent identifier; 'identity_status' says whether the bibliographic identity was checked. "
                 "Neither says anything about whether findings attributed to the report are correct. Imported "
                 "citations are evidence to verify, not verified facts.")


def stages_table(conn: sqlite3.Connection) -> Table:
    rows = [{"stage_key": r["key"], "name": r["name"], "definition": r["definition"], "active": bool(r["active"]),
             "n_papers": r["n_papers"], "n_findings": r["n_findings"]} for r in conn.execute(
        "SELECT s.*, (SELECT COUNT(DISTINCT ps.paper_id) FROM paper_stage ps JOIN paper p ON p.id = ps.paper_id"
        "  WHERE ps.stage_id = s.id AND ps.status = 'active' AND p.record_status = 'active') AS n_papers,"
        " (SELECT COUNT(DISTINCT fs.finding_id) FROM finding_stage fs JOIN finding f ON f.id = fs.finding_id"
        "  WHERE fs.stage_id = s.id AND fs.status = 'active' AND f.record_status = 'active') AS n_findings"
        " FROM stage s ORDER BY s.position")]
    return Table("stages", "Stages", ["stage_key", "name", "definition", "active", "n_papers", "n_findings"], rows,
                 "Project-defined top-level categories. A paper or finding can belong to several stages, so the "
                 "counts do not add up to the totals.")


def relations_table(conn: sqlite3.Connection) -> Table:
    cols = ["relation_id", "finding_a", "finding_b", "relation_type", "status", "method", "score", "decided_by"]
    rows = [{"relation_id": r["id"], **{c: r[c] for c in cols[1:]}} for r in conn.execute(
        "SELECT * FROM finding_relation ORDER BY relation_type, created_at, id")]
    return Table("finding_relations", "Relations between findings", cols, rows,
                 "possible_duplicate: the same statement may have been recorded twice (candidates are never "
                 "deleted or merged automatically). dependent: findings that are not independent observations. "
                 "limited_by: a limitation recorded for the finding. ambiguous_identity: a re-import could not "
                 "tell which earlier finding a changed row corresponds to.",
                 links={"finding_a": "findings", "finding_b": "findings"})


def unparsed_table(conn: sqlite3.Connection) -> Table:
    cols = ["workbook_version", "sheet", "cell_ref", "char_start", "char_end", "reason", "text"]
    rows = [{"workbook_version": f"{r['workbook_key']} v{r['version_no']}", "sheet": r["sheet"],
             "cell_ref": r["cell_ref"], "char_start": r["char_start"], "char_end": r["char_end"],
             "reason": r["reason"], "text": locations.short_excerpt(r["text"])} for r in conn.execute(
        "SELECT u.*, wv.workbook_key, wv.version_no FROM unparsed_content u JOIN workbook_version wv ON"
        " wv.id = u.workbook_version_id WHERE wv.id IN (SELECT id FROM workbook_version w2 WHERE w2.version_no ="
        " (SELECT MAX(version_no) FROM workbook_version w3 WHERE w3.workbook_key = w2.workbook_key))"
        " ORDER BY wv.workbook_key, u.sheet, u.cell_ref, u.char_start")]
    return Table("unparsed_content", "Workbook content that was not turned into structured records", cols, rows,
                 "Content of the latest workbook version that the importer read but could not structure. It is "
                 "retained in the database, not discarded.")


def imports_table(conn: sqlite3.Connection) -> Table:
    cols = ["workbook_key", "version_no", "original_filename", "sha256", "size_bytes", "declared_data_class",
            "imported_at", "imported_by", "stored_path"]
    rows = [{c: r[c] for c in cols} for r in conn.execute(
        "SELECT * FROM workbook_version ORDER BY workbook_key, version_no")]
    return Table("workbook_versions", "Imported workbook versions", cols, rows,
                 "Each imported workbook file is preserved byte-for-byte in the project data directory; sha256 is "
                 "the hash of that file.")


def project_info(project: Project) -> dict[str, Any]:
    """Facts every export states up front: data class, protocol status, counts and what they do not mean."""
    conn = project.conn
    pv = protocol.current(conn)
    return {
        "project_name": project.name,
        "project_id": project.id,
        "data_class": project.data_class,
        "protocol_version": pv.label,
        "protocol_confirmed": pv.is_confirmed,
        "counts": query.counts(conn),
        "audit_events": conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0],
    }


def evidence_tables(project: Project, flt: query.FindingFilter | None = None) -> list[Table]:
    """Papers, findings and their provenance (the linked core tables)."""
    conn = project.conn
    return [papers_table(conn), findings_table(conn, flt), locations_table(conn, flt), stages_table(conn),
            relations_table(conn), unparsed_table(conn), imports_table(conn)]


def summary_tables(project: Project, flt: query.FindingFilter | None = None) -> list[Table]:
    """Evidence maps, missing-information summary, modelling inventory and the search/screening flow as tables.

    These are the same functions the CLI prints and the figures draw (imported here because the report
    modules build on this one).
    """
    from .. import screening
    from ..reports import flow, maps, missing

    conn = project.conn
    return [maps.map_table(maps.evidence_map(conn, "system_context", flt=flt)),
            maps.map_table(maps.evidence_map(conn, "outcome", flt=flt)),
            missing.missing_table(missing.missing_information(conn, flt)),
            missing.modeling_table(missing.modeling_inventory(conn)),
            flow.flow_table(screening.summary(conn))]


def standard_tables(project: Project, flt: query.FindingFilter | None = None) -> list[Table]:
    """Every table written by ``geodesic export``: evidence, workflow records and summaries."""
    from .more_tables import workflow_tables

    return [*evidence_tables(project, flt), *workflow_tables(project.conn), *summary_tables(project, flt)]
