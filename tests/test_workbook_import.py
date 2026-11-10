"""Workbook import: preservation, provenance to cells and spans, reconciliation, synthetic/real separation."""

from __future__ import annotations

import hashlib
import shutil

import pytest

from conftest import OPERATOR, finding_by_wording
from geodesic import audit, locations
from geodesic.errors import MappingError, ProvenanceError, UnconfirmedMappingError
from geodesic.project import create_project, open_project
from geodesic.synthetic.workbook import example_config, fixture_mapping, true_counts
from geodesic.util import loads
from geodesic.workbook.importer import format_reconciliation, import_workbook
from geodesic.workbook.reader import read_workbook


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ preservation
def test_original_workbook_is_preserved_byte_identical(project, workbook_v1, mapping):
    before_bytes = workbook_v1.read_bytes()
    before_mtime = workbook_v1.stat().st_mtime_ns
    result = import_workbook(project, workbook_v1, mapping, actor=OPERATOR)

    # the input file is untouched
    assert workbook_v1.read_bytes() == before_bytes
    assert workbook_v1.stat().st_mtime_ns == before_mtime

    row = project.conn.execute("SELECT * FROM workbook_version WHERE id = ?", (result.workbook_version_id,)).fetchone()
    stored = project.abs(row["stored_path"])
    assert stored.is_file() and stored.parent == project.workbooks_dir
    assert stored.read_bytes() == before_bytes                     # byte-identical copy in the project directory
    assert row["sha256"] == hashlib.sha256(before_bytes).hexdigest() == _sha(stored)
    assert row["size_bytes"] == len(before_bytes)
    assert row["original_filename"] == workbook_v1.name
    assert not stored.stat().st_mode & 0o222                       # stored read-only


def test_tampered_preserved_copy_is_detected(imported, workbook_v1, mapping):
    project, result = imported
    row = project.conn.execute("SELECT stored_path FROM workbook_version").fetchone()
    stored = project.abs(row["stored_path"])
    stored.chmod(0o644)
    stored.write_bytes(stored.read_bytes() + b"x")
    with pytest.raises(Exception, match="no longer matches its SHA-256"):
        import_workbook(project, workbook_v1, mapping, actor=OPERATOR)


# ------------------------------------------------------------------ provenance to cells and spans
def test_every_finding_traces_to_workbook_cells_and_spans(imported):
    project, result = imported
    conn = project.conn
    wb_row = conn.execute("SELECT * FROM workbook_version").fetchone()
    wb = read_workbook(project.abs(wb_row["stored_path"]))
    findings = conn.execute("SELECT * FROM finding").fetchall()
    assert len(findings) == 32
    for f in findings:
        assert f["paper_id"], "a finding must link to a paper"
        locs = locations.current_locations(conn, "finding", f["id"])
        primary = [l for l in locs if l["role"] == "supporting_excerpt"]
        assert len(primary) == 1, f"{f['id']} needs exactly one primary source location"
        for loc in locs:
            assert loc["kind"] == "workbook_cell"
            assert loc["workbook_version_id"] == wb_row["id"]
            assert loc["sheet"] and loc["cell_ref"]
            assert loc["resolves"] == 1
            ok, note, _ = locations.resolve(conn, loc)                 # re-check against stored cell text
            assert ok, f"{f['id']} {loc['role']} does not resolve: {note}"
            # and independently against the preserved workbook file itself
            cell = next(c for c in wb.sheet(loc["sheet"]).cells.values() if c.cell_ref == loc["cell_ref"])
            if loc["char_start"] is not None:
                assert cell.raw_text[loc["char_start"]:loc["char_end"]] == loc["excerpt"]
        p = primary[0]
        assert f["source_wording"] == p["excerpt"]


def test_literal_values_are_grounded_in_the_source_span(imported):
    project, _ = imported
    conn = project.conn
    n_checked = 0
    for f in conn.execute("SELECT * FROM finding WHERE kind = 'measurement'"):
        spans = loads(f["spans_json"], {})
        locs = {l["role"]: l for l in locations.current_locations(conn, "finding", f["id"])}
        primary = locs["supporting_excerpt"]
        cell_text = locations.unit_text(conn, primary)[0]
        for field in ("value_literal", "unit_original", "uncertainty_literal", "timepoint_literal",
                      "sample_size_literal"):
            literal = f[field]
            if literal is None:
                continue
            span = spans[field]
            if isinstance(span, dict):
                text = locations.unit_text(conn, locs[span["role"]])[0]
                start, end = span["start"], span["end"]
            else:
                text, (start, end) = cell_text, span
            assert text[start:end] == literal, f"{f['id']}.{field}: {literal!r} is not at its span"
            n_checked += 1
        assert f["grounding_status"] == "grounded"
    assert n_checked >= 30


def test_cell_text_hyperlinks_and_comments_are_stored(imported):
    project, _ = imported
    conn = project.conn
    cell = conn.execute("SELECT * FROM source_cell WHERE sheet = 'Release' AND cell_ref = 'D4'").fetchone()
    assert cell["raw_text"].startswith("Tracer release was approximately 42 %")
    assert cell["comment"] == "Value read from the summary table, not the text."
    n_links = conn.execute("SELECT COUNT(*) FROM source_cell WHERE hyperlink IS NOT NULL").fetchone()[0]
    assert n_links >= 1
    # the DOI that exists only as a hyperlink target was picked up and validated syntactically
    paper = conn.execute("SELECT * FROM paper WHERE doi = '10.5555/geodesic.synth.004'").fetchone()
    assert paper is not None and paper["resolution_status"] == "resolved"
    # unknown (unmapped) columns are retained as cells, and unparsed content is kept with its reason
    assert conn.execute("SELECT COUNT(*) FROM source_cell WHERE sheet = 'Release' AND cell_ref = 'G4'"
                        ).fetchone()[0] == 1
    reasons = {r["reason"] for r in conn.execute("SELECT reason FROM unparsed_content")}
    assert reasons == {"ambiguous_decimal_comma", "no_quantity_in_measurement_cell", "row_without_paper_reference"}


def test_merged_reference_cells_apply_to_the_rows_they_span(imported):
    project, _ = imported
    conn = project.conn
    half_time = finding_by_wording(conn, "Release half-time was 4.5 h")
    first = finding_by_wording(conn, "Release rate stayed > 5 mL/min")
    assert half_time["paper_id"] == first["paper_id"]
    locs = {l["role"]: l for l in locations.current_locations(conn, "finding", half_time["id"])}
    assert locs["supporting_excerpt"]["cell_ref"] == "D5"
    # the paper itself is traced to the merged range's anchor cell
    cites = [l for l in locations.current_locations(conn, "paper", half_time["paper_id"]) if l["role"] == "citation"]
    assert [(l["sheet"], l["cell_ref"]) for l in cites] == [("Release", "A4")]
    assert cites[0]["resolves"] == 1


# ------------------------------------------------------------------ papers and identifiers
def test_paper_references_and_identifier_handling(imported):
    project, result = imported
    conn = project.conn
    papers = {r["id"]: r for r in conn.execute("SELECT * FROM paper")}
    assert len(papers) == 8
    by_status = {}
    for p in papers.values():
        by_status.setdefault(p["resolution_status"], []).append(p)
    assert len(by_status["resolved"]) == 6 and len(by_status["unresolved"]) == 2
    dois = sorted(p["doi"] for p in papers.values() if p["doi"])
    assert "10.5555/geodesic.synth.007" in dois                   # URL-form, upper-case DOI was normalised
    assert "10.5555/geodesic.synth.008" in dois                   # "doi:" prefix removed
    assert all(d == d.lower() for d in dois)
    # an identifier is only a syntactic match: nothing is marked identity-verified by the import
    assert {p["identity_status"] for p in papers.values()} <= {"unverified", "conflict"}
    # invalid identifier: kept as written, flagged, paper left unresolved
    invalid = conn.execute("SELECT * FROM paper_identifier WHERE validation_status = 'invalid_syntax'").fetchall()
    assert len(invalid) == 1 and invalid[0]["value_raw"] == "10.555/bad"
    assert papers[invalid[0]["paper_id"]]["resolution_status"] == "unresolved"
    # conflicting DOIs for one citation: both preserved, conflict open, identity flagged
    conflicts = conn.execute("SELECT * FROM metadata_conflict").fetchall()
    assert len(conflicts) == 1 and conflicts[0]["field"] == "doi" and conflicts[0]["status"] == "open"
    assert papers[conflicts[0]["paper_id"]]["identity_status"] == "conflict"
    assert result.stats["papers"]["invalid_identifiers"] == 1
    assert result.stats["papers"]["metadata_conflicts"] == 1


def test_stage_assignments_are_recorded_as_imported(imported):
    project, _ = imported
    conn = project.conn
    sources = {r[0] for r in conn.execute("SELECT DISTINCT assignment_source FROM finding_stage")}
    assert sources == {"imported"}
    assert {r[0] for r in conn.execute("SELECT DISTINCT assignment_source FROM paper_stage")} == {"imported"}
    # a paper cited on several stage sheets belongs to several stages
    n = conn.execute(
        "SELECT MAX(n) FROM (SELECT COUNT(DISTINCT stage_id) AS n FROM paper_stage WHERE status = 'active'"
        " GROUP BY paper_id)").fetchone()[0]
    assert n == 3
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE id NOT IN (SELECT finding_id FROM finding_stage)"
                        ).fetchone()[0] == 0


# ------------------------------------------------------------------ reconciliation
def test_reconciliation_counts_match_the_fixture(imported):
    project, result = imported
    s = result.stats
    truth = true_counts(1)
    assert s["cells_read"] == 125
    assert s["evidence_rows"] == truth["__all__"]["evidence_rows"] == 14
    assert s["evidence_rows_by_sheet"] == {k: v["evidence_rows"] for k, v in truth.items() if k != "__all__"}
    assert s["papers"]["referenced"] == truth["__all__"]["distinct_papers"] == 8
    assert s["papers"]["created"] == 8 and s["papers"]["unresolved"] == 2
    assert s["findings"]["in_workbook"] == 32
    assert s["findings"]["by_kind"] == {"limitation": 5, "measurement": 20, "narrative": 7}
    assert s["findings"]["by_statement_type"] == {"approximate": 3, "bound": 4, "exact": 9, "qualitative": 11,
                                                  "range": 4, "unparsed": 1}
    assert s["findings"]["added"] == 32
    assert s["unparsed_content"]["total"] == 3
    assert s["parse_failures"] == 0
    assert s["requiring_review"] == {"draft": 1, "flagged": 17, "unsupported": 0, "stale": 0, "unresolved_papers": 2}
    assert s["possible_repeated_finding_pairs"] == 1
    # the workbook's own summary sheet deliberately disagrees in two cells; the importer must say so
    rec = {(r["cell"], r["measures"]): r for r in s["reconciliation"]}
    assert len(rec) == 10
    mismatches = sorted(c for (c, _), r in rec.items() if r["status"] == "mismatch")
    assert mismatches == ["B4", "B6"]
    assert rec[("B4", "evidence_rows")]["workbook_count"] == 4 and rec[("B4", "evidence_rows")]["imported_count"] == 3
    assert rec[("B6", "evidence_rows")]["workbook_count"] == 15 and rec[("B6", "evidence_rows")]["imported_count"] == 14
    text = format_reconciliation(s, with_definitions=True)
    assert "MISMATCH" in text and "Definitions:" in text and "requiring_review.draft" in text
    # database agrees with the reported counts
    assert project.conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 32
    assert project.conn.execute("SELECT COUNT(*) FROM source_cell").fetchone()[0] == 125


def test_repeated_findings_are_flagged_not_deleted(imported):
    project, _ = imported
    conn = project.conn
    rels = conn.execute("SELECT * FROM finding_relation WHERE relation_type = 'possible_duplicate'").fetchall()
    assert len(rels) == 1 and rels[0]["status"] == "candidate"
    a = conn.execute("SELECT * FROM finding WHERE id = ?", (rels[0]["finding_a"],)).fetchone()
    b = conn.execute("SELECT * FROM finding WHERE id = ?", (rels[0]["finding_b"],)).fetchone()
    assert a["record_status"] == b["record_status"] == "active"
    assert "approximately 42 %" in a["source_wording"] and "approximately 42 %" in b["source_wording"]


# ------------------------------------------------------------------ mapping gate
def test_importer_refuses_unconfirmed_mapping(project, workbook_v1):
    with pytest.raises(UnconfirmedMappingError) as err:
        import_workbook(project, workbook_v1, fixture_mapping(confirmed=False), actor=OPERATOR)
    assert "confirmed: true" in str(err.value)
    assert project.conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 0
    assert project.conn.execute("SELECT COUNT(*) FROM workbook_version").fetchone()[0] == 0


def test_one_unconfirmed_required_entry_is_enough_to_refuse(project, workbook_v1, mapping):
    mapping.sheet("Transit").columns_with("narrative_findings")[0].confirmed = False
    with pytest.raises(UnconfirmedMappingError, match="Transit.*narrative_findings"):
        import_workbook(project, workbook_v1, mapping, actor=OPERATOR)


def test_mapping_that_does_not_fit_the_workbook_is_rejected(project, workbook_v1, mapping):
    mapping.sheets = [s for s in mapping.sheets if s.sheet != "README"]
    mapping.sheet("Release").stage.stage_keys = ["no_such_stage"]
    with pytest.raises(MappingError) as err:
        import_workbook(project, workbook_v1, mapping, actor=OPERATOR)
    assert not isinstance(err.value, UnconfirmedMappingError)
    assert "README" in str(err.value) and "no_such_stage" in str(err.value)


# ------------------------------------------------------------------ synthetic / real separation
def test_synthetic_fixture_is_rejected_by_a_real_project(real_project, workbook_v1, mapping):
    with pytest.raises(ProvenanceError, match="declared 'synthetic' but this project is 'real'"):
        import_workbook(real_project, workbook_v1, mapping, actor=OPERATOR)
    with pytest.raises(ProvenanceError, match="cannot be imported as real evidence"):
        import_workbook(real_project, workbook_v1, mapping, declare="real", actor=OPERATOR)
    assert real_project.conn.execute("SELECT COUNT(*) FROM workbook_version").fetchone()[0] == 0
    assert not any(real_project.workbooks_dir.glob("*.xlsx")) if real_project.workbooks_dir.exists() else True


def _untagged_copy(tmp_path, workbook_v1):
    """A generated workbook without the embedded synthetic tag (stands in for a user's untagged file)."""
    import openpyxl

    wb = openpyxl.load_workbook(workbook_v1)
    for prop in list(wb.custom_doc_props.props):
        del wb.custom_doc_props[prop.name]
    out = tmp_path / "untagged" / "untagged.xlsx"
    out.parent.mkdir()
    wb.save(out)
    return out


def test_untagged_import_requires_a_declaration(project, real_project, tmp_path, workbook_v1, mapping):
    untagged = _untagged_copy(tmp_path, workbook_v1)
    with pytest.raises(ProvenanceError, match="carries no provenance tag"):
        import_workbook(project, untagged, mapping, actor=OPERATOR)
    with pytest.raises(ProvenanceError, match="declared 'real' but this project is 'synthetic'"):
        import_workbook(project, untagged, mapping, declare="real", actor=OPERATOR)
    with pytest.raises(ProvenanceError, match="declared 'synthetic' but this project is 'real'"):
        import_workbook(real_project, untagged, mapping, declare="synthetic", actor=OPERATOR)
    ok = import_workbook(project, untagged, mapping, declare="synthetic", actor=OPERATOR)
    assert ok.status == "imported"
    row = project.conn.execute("SELECT declared_data_class, embedded_tag FROM workbook_version").fetchone()
    assert row["declared_data_class"] == "synthetic" and row["embedded_tag"] is None
    # a declared-real import of an untagged generated file into a real-class project is accepted
    ok2 = import_workbook(real_project, untagged, mapping, declare="real", actor=OPERATOR)
    assert ok2.stats["findings"]["in_workbook"] == 32


def test_project_data_class_is_immutable(project, tmp_path):
    with pytest.raises(Exception, match="immutable|data_class"):
        project.conn.execute("UPDATE project SET data_class = 'real'")
    assert project.conn.execute("SELECT data_class FROM project").fetchone()[0] == "synthetic"
    root = project.root
    project.close()
    reopened = open_project(root)
    assert reopened.data_class == "synthetic" and reopened.is_synthetic
    reopened.close()
    with pytest.raises(Exception):
        create_project(root, example_config(), "real", OPERATOR)       # an existing project is never re-created


def test_import_is_audited_and_chain_verifies(imported):
    project, result = imported
    ok, n, bad = audit.verify_chain(project.conn)
    assert ok and bad is None and n >= 5
    actions = [r["action"] for r in audit.history(project.conn, limit=500)]
    assert actions.count("workbook.import") == 1
    # each finding has its first version recorded with the import that created it
    kinds = project.conn.execute("SELECT change_kind, COUNT(*) FROM finding_version GROUP BY 1").fetchall()
    assert [tuple(k) for k in kinds] == [("imported", 32)]
