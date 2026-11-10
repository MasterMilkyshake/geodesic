"""Search and filtering, export integrity, the profiler and starter mapping, and the M1 command line."""

from __future__ import annotations

import csv
import hashlib
import json

import openpyxl
import pandas as pd
import pytest
import yaml

from conftest import OPERATOR, finding_by_wording
from geodesic import query, review
from geodesic.cli import main
from geodesic.config import dump_yaml
from geodesic.errors import GeodesicError, MappingError, UnconfirmedMappingError, ValidationError
from geodesic.export import export_tables
from geodesic.project import create_project
from geodesic.synthetic.workbook import example_config, fixture_mapping, write_fixture_workbook
from geodesic.workbook.importer import import_workbook
from geodesic.workbook.mapping import dump_mapping, load_mapping, write_mapping
from geodesic.workbook.profiler import profile_workbook, render_profile_md, starter_mapping
from geodesic.workbook.reader import read_workbook

CHECKS = {c: "confirmed" for c in review.REQUIRED_CHECKS}
BANNER = "SYNTHETIC DATA: generated test fixture, not research evidence"


# ------------------------------------------------------------------ search and filtering
def test_filters_and_pagination(imported, reviewer_a):
    project, _ = imported
    conn = project.conn
    F = query.FindingFilter
    everything = query.search_findings(conn, limit=None)
    assert everything.total == 32 and len(everything.rows) == 32 and not everything.has_more

    # pagination is done by the database and pages do not overlap
    seen = []
    for offset in (0, 10, 20, 30):
        page = query.search_findings(conn, limit=10, offset=offset)
        assert page.total == 32 and page.has_more == (offset + 10 < 32)
        seen += [r["id"] for r in page.rows]
    assert len(seen) == len(set(seen)) == 32

    by_kind = {k: query.search_findings(conn, F(kinds=[k])).total for k in ("measurement", "narrative", "limitation")}
    assert by_kind == {"measurement": 20, "narrative": 7, "limitation": 5}
    assert query.search_findings(conn, F(quantitative=True)).total == 20
    assert query.search_findings(conn, F(quantitative=False)).total == 12
    assert query.search_findings(conn, F(statement_types=["bound"])).total == 4
    assert query.search_findings(conn, F(statement_types=["range", "bound"])).total == 8

    # stage filter; a finding may be in several stages, so the per-stage totals need not add up to 32
    per_stage = {k: query.search_findings(conn, F(stage_keys=[k])).total
                 for k in ("release", "transit", "retention", "discharge")}
    assert all(n > 0 for n in per_stage.values())
    assert query.search_findings(conn, F(stage_keys=list(per_stage))).total == 32
    assert query.search_findings(conn, F(unassigned_stage=True)).total == 0

    with_u = query.search_findings(conn, F(has_uncertainty=True)).total
    without_u = query.search_findings(conn, F(has_uncertainty=False)).total
    assert with_u > 0 and with_u + without_u == 32
    with_t = query.search_findings(conn, F(has_timepoint=True)).total
    assert 0 < with_t < 32
    assert query.search_findings(conn, F(system_context="source tank")).total > 0
    assert query.search_findings(conn, F(relative_semantics=["unspecified_relative"])).total > 0
    assert query.search_findings(conn, F(hierarchy_unassigned=True)).total == 32

    # review status
    f = finding_by_wording(conn, "Release half-time was 4.5 h")
    review.verify(conn, f["id"], reviewer_a, CHECKS)
    verified = query.search_findings(conn, F(review_statuses=["verified"]))
    assert [r["id"] for r in verified.rows] == [f["id"]]
    assert query.search_findings(conn, F(review_statuses=["draft"])).total == 1
    assert query.search_findings(conn, F(flags=["uncertainty_type_unspecified"])).total >= 1

    # rows carry their paper and stage context
    row = verified.rows[0]
    assert row["paper_citation"] and row["stages"] == ["release"] and row["stage_sources"] == {"release": ["imported"]}
    with pytest.raises(ValidationError, match="order must be one of"):
        query.search_findings(conn, order="best")


def test_full_text_search(imported):
    project, _ = imported
    conn = project.conn
    F = query.FindingFilter
    hits = query.search_findings(conn, F(text="half-time"))
    assert hits.total >= 1 and all("half-time" in r["source_wording"].lower() for r in hits.rows)
    assert query.search_findings(conn, F(text="bypass retention")).total >= 1        # all words must match
    assert query.search_findings(conn, F(text="bypass zzzunknownzzz")).total == 0
    assert query.search_findings(conn, F(text="disper*")).total >= 1                  # prefix search
    # query syntax characters are treated as text, not as operators
    assert query.search_findings(conn, F(text='"tracer" OR NOT (')).total == 0
    with pytest.raises(ValidationError, match="no searchable words"):
        query.search_findings(conn, F(text="   "))
    by_paper = query.search_findings(conn, F(paper_text="Kestrel"))
    assert by_paper.total >= 1 and {r["paper_citation"].split()[0] for r in by_paper.rows} == {"Kestrel"}


def test_paper_filters_and_counts(imported):
    project, _ = imported
    conn = project.conn
    P = query.PaperFilter
    assert query.search_papers(conn).total == 8
    assert query.search_papers(conn, P(resolution_statuses=["unresolved"])).total == 2
    assert query.search_papers(conn, P(has_open_conflicts=True)).total == 1
    assert query.search_papers(conn, P(origins=["search"])).total == 0
    assert query.search_papers(conn, P(year_min=2019)).total < 8
    assert query.search_papers(conn, P(screening_stage="title_abstract", screening_outcomes=["unscreened"])).total == 8
    page = query.search_papers(conn, P(has_findings=True), limit=3)
    assert len(page.rows) == 3 and page.has_more and all(r["n_findings"] > 0 for r in page.rows)

    c = query.counts(conn)
    # reports, studies, experiments and findings are counted separately, and incomplete assignment is visible
    assert (c["reports"], c["studies"], c["experiments"], c["findings"]) == (8, 0, 0, 32)
    assert c["findings_without_experiment"] == 32 and c["reports_unresolved"] == 2
    values = dict(query.distinct_values(conn, "unit_original"))
    assert "%" in values and "mL/min" in values
    with pytest.raises(ValidationError):
        query.distinct_values(conn, "id; DROP TABLE finding")


# ------------------------------------------------------------------ exports
def test_export_integrity_and_source_locations(imported, tmp_path, reviewer_a):
    project, _ = imported
    conn = project.conn
    out = export_tables(project, tmp_path / "out", formats=("xlsx", "csv", "json", "ris"), actor=OPERATOR)
    names = sorted(p.name for p in out["files"])
    assert all(n.startswith("SYNTHETIC_") for n in names) and out["manifest"].name == "SYNTHETIC_manifest.json"
    assert out["rows"]["findings"] == 32 and out["rows"]["papers"] == 8

    # Excel workbook: banner in row 1, header in row 2, linked tables
    xlsx = tmp_path / "out" / "SYNTHETIC_evidence.xlsx"
    wb = openpyxl.load_workbook(xlsx)
    assert wb.sheetnames[:4] == ["README", "papers", "findings", "source_locations"]
    for ws in wb:
        assert str(ws.cell(1, 1).value).startswith(BANNER)
    assert "PROVISIONAL" in str(wb["findings"].cell(1, 1).value)
    findings = pd.read_excel(xlsx, sheet_name="findings", header=1)
    locs = pd.read_excel(xlsx, sheet_name="source_locations", header=1)
    papers = pd.read_excel(xlsx, sheet_name="papers", header=1)
    db_ids = {r[0] for r in conn.execute("SELECT id FROM finding")}
    assert set(findings["finding_id"]) == db_ids                           # stable internal IDs survive export
    assert set(findings["paper_id"]) <= set(papers["paper_id"])            # linked tables
    assert set(locs["finding_id"]) == db_ids                               # every finding has a source location
    link = wb["findings"].cell(3, 2).hyperlink
    assert link is not None and link.location.startswith("'papers'!A")

    # each exported finding shows its citation, source location and a short excerpt
    primary = locs[locs["role"] == "supporting_excerpt"].set_index("finding_id")
    assert len(primary) == 32
    for fid, row in findings.set_index("finding_id").iterrows():
        loc = primary.loc[fid]
        assert row["citation"] and loc["excerpt"] == row["source_wording"]
        assert loc["location"].startswith("main v1")                       # workbook key and version
        assert bool(loc["resolves"])
    sample = primary.loc[finding_by_wording(conn, "Release half-time was 4.5 h")["id"]]
    assert sample["location"] == "main v1 · Release!D5 chars 0-34"
    assert locs["excerpt"].astype(str).str.len().max() <= 500              # excerpts, not whole documents
    for col in ("review_status", "grounding_status", "extraction_method", "interpretation_limits", "review_flags"):
        assert col in findings.columns                                    # status and limits travel with evidence

    # CSV: same rows, values exactly as stored, data class in every row
    with open(tmp_path / "out" / "SYNTHETIC_findings.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 32 and {r["data_class"] for r in rows} == {"synthetic"}
    by_id = {r["finding_id"]: r for r in rows}
    for r in conn.execute("SELECT id, source_wording, value_literal, unit_original FROM finding"):
        assert by_id[r["id"]]["source_wording"] == r["source_wording"]
        assert by_id[r["id"]]["value_literal"] == (r["value_literal"] or "")
        assert by_id[r["id"]]["unit_original"] == (r["unit_original"] or "")

    # JSON and RIS
    data = json.loads((tmp_path / "out" / "SYNTHETIC_evidence.json").read_text(encoding="utf-8"))
    assert data["label"].startswith(BANNER) and data["project"]["data_class"] == "synthetic"
    assert len(data["tables"]["findings"]["rows"]) == 32
    assert "finding_id" in data["tables"]["findings"]["columns"]
    ris = (tmp_path / "out" / "SYNTHETIC_references.ris").read_text(encoding="utf-8")
    assert ris.count("TY  - ") == 8 == ris.count("ER  - ")
    assert "DO  - 10.5555/geodesic.synth.007" in ris and BANNER in ris
    assert "correction/retraction status not checked" in ris              # never "not retracted"

    # manifest: hashes of every file and of the preserved inputs
    manifest = json.loads(out["manifest"].read_text(encoding="utf-8"))
    assert manifest["project"]["data_class"] == "synthetic" and manifest["protocol_confirmed"] is False
    for entry in manifest["files"]:
        assert hashlib.sha256((tmp_path / "out" / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
    assert {e["name"] for e in manifest["files"]} == set(names)
    wb_hash = conn.execute("SELECT sha256 FROM workbook_version").fetchone()[0]
    assert [w["sha256"] for w in manifest["inputs"]["workbook_versions"]] == [wb_hash]
    cfg = json.loads((tmp_path / "out" / "SYNTHETIC_project_configuration.json").read_text(encoding="utf-8"))
    assert [s["key"] for s in cfg["project_config"]["stages"]] == ["release", "transit", "retention", "discharge"]
    assert cfg["protocol"]["status"] == "provisional" and len(cfg["workbook_mappings"]) == 1
    assert "contact_email" not in json.dumps(cfg) and "mailto" not in json.dumps(cfg)   # local settings stay local


def test_filtered_export_and_unknown_format(imported, tmp_path):
    project, _ = imported
    out = export_tables(project, tmp_path / "bounds", formats=("csv",),
                        flt=query.FindingFilter(statement_types=["bound"]))
    assert out["rows"]["findings"] == 4
    with open(tmp_path / "bounds" / "SYNTHETIC_findings.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert {r["statement_type"] for r in rows} == {"bound"}
    assert all(r["value_num"] == "" and r["bound_type"] for r in rows)     # a bound is never exported as a point value
    with pytest.raises(GeodesicError, match="Unknown export format"):
        export_tables(project, tmp_path / "x", formats=("docx",))


def test_real_project_exports_are_unlabelled_and_refused_inside_source_control(real_project, tmp_path):
    out = export_tables(real_project, tmp_path / "real_out", formats=("csv",))
    assert not any(p.name.startswith("SYNTHETIC_") for p in out["files"])
    repo = tmp_path / "some_repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(GeodesicError, match="Refusing to export real project data"):
        export_tables(real_project, repo / "exports", formats=("csv",))
    assert not (repo / "exports").exists()


def test_real_project_cannot_be_created_inside_source_control(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(GeodesicError):
        create_project(repo / "data", example_config("Declared-real test project"), "real", OPERATOR)


# ------------------------------------------------------------------ profiler and starter mapping
def test_profile_reports_structure_without_guessing_meaning(workbook_v1):
    before = workbook_v1.read_bytes()
    profile = profile_workbook(workbook_v1)
    assert workbook_v1.read_bytes() == before                              # profiling is read-only
    assert profile["sha256"] == hashlib.sha256(before).hexdigest()
    sheets = {s["sheet"]: s for s in profile["sheets"]}
    assert list(sheets) == ["README", "Release", "Transit", "Retention", "Discharge", "Summary"]
    release = sheets["Release"]
    assert release["header_row_detected"] == 3 and release["data_start_row_detected"] == 4
    assert release["used_rows"] == 8 and release["used_columns"] == 8
    headers = [c["header"] for c in release["columns"]]
    assert headers[0] == "Reference" and len(headers) == 8
    assert release["columns"][0]["value_types"] == {"str": 3} and release["columns"][0]["cells_covered_by_merges"] == 1
    assert release["merged_ranges"] == 2 and release["merged_range_examples"] == ["A4:A5", "B4:B5"]
    assert release["comments"] == 1
    assert sum(s["hyperlinks"] for s in sheets.values()) >= 1
    assert sum(s["formula_cells"] for s in sheets.values()) >= 1
    # formatting that may carry meaning is reported as found, with no interpretation attached
    colors = {f["color"]: f for f in release["fill_colors"]}
    assert colors["00FFFF00"]["cells"] == 1 and colors["00FFFF00"]["columns"] == ["E"]
    assert set(colors["00FFFF00"]) == {"color", "cells", "columns", "rows", "header_cells"}
    text = render_profile_md(profile)
    assert "## Sheet `Release`" in text and "Merged ranges: 2" in text
    assert "Fill colours (meaning not interpreted)" in text and "detected, unconfirmed" in text
    assert profile["embedded_synthetic_tag"] == "synthetic"


def test_starter_mapping_is_entirely_unconfirmed_and_blocks_import(project, workbook_v1, tmp_path):
    wb = read_workbook(workbook_v1)
    profile = profile_workbook(workbook_v1, wb)
    stages = [{"key": s.key, "name": s.name} for s in project.config.stages]
    mapping = starter_mapping(profile, wb, stages)
    assert len(mapping.sheets) == 6
    for sheet in mapping.sheets:
        assert sheet.confirmed is False
        assert all(not c.confirmed for c in sheet.columns)
        assert sheet.stage.confirmed is False
    assert all(not c.confirmed for c in mapping.summary_counts)
    roles = {s.sheet: s.role for s in mapping.sheets}
    assert roles["Summary"] == "summary" and roles["Release"] == "evidence"

    path = tmp_path / "starter.yaml"
    write_mapping(mapping, path)
    text = path.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert "confirmed: false" in body and "confirmed: true" not in body
    with pytest.raises(UnconfirmedMappingError) as err:
        import_workbook(project, workbook_v1, load_mapping(path), actor=OPERATOR)
    assert "unconfirmed" in str(err.value)
    assert project.conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 0

    # the hand-confirmed fixture mapping round-trips through the YAML file format
    path2 = tmp_path / "confirmed.yaml"
    path2.write_text(dump_mapping(fixture_mapping(confirmed=True)), encoding="utf-8")
    assert load_mapping(path2).content_sha256() == fixture_mapping(confirmed=True).content_sha256()


def _single_sheet_workbook(path):
    """A different layout: all stages on one sheet, with the stage named in a column."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Evidence"
    ws.append(["Source", "Step", "What was found"])
    ws.append(["Arden P. Synthetic rig notes. 2020.", "Release", "Released fraction was 12 % at 2 h."])
    ws.append(["Arden P. Synthetic rig notes. 2020.", "Transit; Holding", "Transit time was < 30 min."])
    ws.append(["Brook Q. Synthetic weir notes. 2021.", "Polishing", "Outflow stayed clear."])
    from openpyxl.packaging.custom import StringProperty
    from geodesic.provenance import TAG_PROPERTY
    wb.custom_doc_props.append(StringProperty(name=TAG_PROPERTY, value="synthetic"))
    wb.save(path)
    return path


def _single_sheet_mapping(value_map: dict) -> dict:
    return {
        "mapping_version": 1, "workbook_key": "single",
        "sheets": [{
            "sheet": "Evidence", "role": "evidence", "confirmed": True, "header_row": 1, "data_start_row": 2,
            "stage": {"mode": "column", "separator": ";", "value_map": value_map, "confirmed": True},
            "columns": [
                {"column": "A", "role": "paper_reference", "confirmed": True},
                {"column": "B", "role": "stage", "confirmed": True},
                {"column": "C", "role": "narrative_findings", "confirmed": True},
            ],
        }],
    }


def test_a_new_layout_needs_only_a_new_mapping(project, tmp_path):
    from geodesic.workbook.mapping import WorkbookMapping

    path = _single_sheet_workbook(tmp_path / "single.xlsx")
    try:
        full = WorkbookMapping.model_validate(_single_sheet_mapping(
            {"Release": "release", "Transit": "transit", "Holding": "retention"}))
        partial = WorkbookMapping.model_validate(_single_sheet_mapping(
            {"Release": "release", "Transit": "transit", "Holding": ""}))
        wrong = WorkbookMapping.model_validate(_single_sheet_mapping({"Release": "no_such_stage"}))
    except Exception as exc:                                               # pragma: no cover - layout drift guard
        pytest.fail(f"mapping schema changed: {exc}")

    with pytest.raises(UnconfirmedMappingError, match="Holding"):
        import_workbook(project, path, partial, actor=OPERATOR)             # empty stage key = unconfirmed entry
    with pytest.raises(MappingError, match="no_such_stage"):
        import_workbook(project, path, wrong, actor=OPERATOR)

    result = import_workbook(project, path, full, actor=OPERATOR)
    conn = project.conn
    assert result.stats["evidence_rows"] == 3 and result.stats["papers"]["referenced"] == 2
    two_stage = finding_by_wording(conn, "Transit time was < 30 min")
    keys = sorted(r[0] for r in conn.execute(
        "SELECT s.key FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id WHERE fs.finding_id = ?",
        (two_stage["id"],)))
    assert keys == ["retention", "transit"]                                # one finding, two stages
    assert two_stage["statement_type"] == "bound" and two_stage["value_num"] is None
    # a stage term that the mapping does not cover is kept as unmapped content, never guessed
    unmapped = finding_by_wording(conn, "Outflow stayed clear")
    assert conn.execute("SELECT COUNT(*) FROM finding_stage WHERE finding_id = ?", (unmapped["id"],)).fetchone()[0] == 0
    reasons = [r[0] for r in conn.execute("SELECT reason FROM unparsed_content")]
    assert "unmapped_stage_term" in reasons
    assert query.search_findings(conn, query.FindingFilter(unassigned_stage=True)).total == 1


# ------------------------------------------------------------------ command line
def _write_inputs(tmp_path):
    cfg = tmp_path / "project.yaml"
    dump_yaml(example_config().model_dump(mode="json"), cfg)
    wb = write_fixture_workbook(tmp_path / "wb" / "synthetic_workbook.xlsx", version=1) \
        if (tmp_path / "wb").mkdir() is None else None
    mapping = tmp_path / "mapping.yaml"
    write_mapping(fixture_mapping(confirmed=True), mapping)
    return cfg, wb, mapping


def test_cli_profile_import_and_one_command_export(tmp_path, capsys):
    cfg, wb, mapping = _write_inputs(tmp_path)
    proj = tmp_path / "cli_project"
    assert main(["init", "--project", str(proj), "--config", str(cfg), "--data-class", "synthetic"]) == 0
    assert main(["init", "--project", str(proj), "--config", str(cfg), "--data-class", "synthetic"]) == 2

    # profile + starter mapping
    prof = tmp_path / "profile"
    assert main(["profile-workbook", str(wb), "--out", str(prof), "--project", str(proj)]) == 0
    written = sorted(p.name for p in prof.iterdir())
    assert any(n.endswith(".md") for n in written) and any(n.endswith(".json") for n in written)
    starter = next(p for p in prof.iterdir() if p.suffix in (".yaml", ".yml"))
    capsys.readouterr()

    # the unconfirmed starter mapping is refused with a non-zero exit code
    assert main(["import-workbook", str(wb), "--mapping", str(starter), "--project", str(proj),
                 "--reviewer", "Synthetic Operator"]) == 2
    assert "unconfirmed" in capsys.readouterr().err

    # the confirmed mapping imports and prints reconciliation counts with definitions
    assert main(["import-workbook", str(wb), "--mapping", str(mapping), "--project", str(proj),
                 "--reviewer", "Synthetic Operator"]) == 0
    out = capsys.readouterr().out
    assert "[SYNTHETIC project" in out and "PROVISIONAL" in out and "MISMATCH" in out and "Definitions:" in out
    assert "32" in out and "125" in out
    assert main(["import-workbook", str(wb), "--mapping", str(mapping), "--project", str(proj),
                 "--reviewer", "Synthetic Operator", "--json"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["status"] == "already_imported"

    # one command writes the workbook and the CSV exports
    out_dir = tmp_path / "cli_export"
    assert main(["export", "--project", str(proj), "--out", str(out_dir)]) == 0
    names = sorted(p.name for p in out_dir.iterdir())
    assert "SYNTHETIC_evidence.xlsx" in names and "SYNTHETIC_findings.csv" in names
    assert "SYNTHETIC_papers.csv" in names and "SYNTHETIC_manifest.json" in names
    capsys.readouterr()

    assert main(["findings", "--project", str(proj), "--statement-type", "bound", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["total"] == 4
    assert main(["status", "--project", str(proj), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["data_class"] == "synthetic" and status["audit_chain_intact"] is True
    assert status["protocol"] == "v1 (PROVISIONAL)" and status["counts"]["findings"] == 32
    assert main(["audit", "verify", "--project", str(proj)]) == 0


def test_cli_errors_are_actionable(tmp_path, capsys):
    cfg, wb, mapping = _write_inputs(tmp_path)
    assert main(["status", "--project", str(tmp_path / "missing")]) == 2
    assert "geodesic init" in capsys.readouterr().err
    proj = tmp_path / "real_cli_project"
    assert main(["init", "--project", str(proj), "--config", str(cfg), "--data-class", "real"]) == 0
    capsys.readouterr()
    # a synthetic fixture cannot enter a real project
    assert main(["import-workbook", str(wb), "--mapping", str(mapping), "--project", str(proj),
                 "--reviewer", "Synthetic Operator"]) == 2
    assert "synthetic" in capsys.readouterr().err
    # no reviewer configured anywhere: the command says how to configure one
    proj2 = tmp_path / "syn_cli_project"
    assert main(["init", "--project", str(proj2), "--config", str(cfg), "--data-class", "synthetic"]) == 0
    capsys.readouterr()
    code = main(["protocol", "confirm", "--project", str(proj2)])
    assert code == 2
    assert "reviewer" in capsys.readouterr().err.lower()
