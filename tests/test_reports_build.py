"""HTML report, report directory and the additional export tables."""

from __future__ import annotations

import csv
import hashlib
import json
import re

import pytest

from geodesic import comparison, query, review, screening
from geodesic.errors import GeodesicError
from geodesic.export import export_tables
from geodesic.export import tables as T
from geodesic.extraction import validation
from geodesic.reports import build, html, maps, missing

from conftest import ALL_CONFIRMED, OPERATOR, REVIEWER_A

BANNER = "SYNTHETIC DATA"


def _text(page: str) -> str:
    """Visible text of the report (tags removed, entities kept)."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))


def test_report_is_self_contained_and_labelled(corpus):
    page = html.render_report(corpus)
    assert page.startswith("<!DOCTYPE html>")
    # no scripts, no external resources, CSP as a second line of defence
    assert "<script" not in page.lower()
    assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", page)
    assert "default-src 'none'" in page
    assert page.count("data:image/svg+xml;base64,") == 2          # flow and missing-information figures
    text = _text(page)
    assert text.count(BANNER) >= 2                                  # top banner and closing line
    assert "Everything in this report is synthetic" in text
    assert "The protocol is provisional" in text and "PROVISIONAL" in text
    assert "no compliance with a reporting guideline is claimed" in text
    assert "The application cannot detect undisclosed synthetic content" in text
    assert "correction/retraction status not checked" in text
    assert "not retracted" not in text.lower()
    for section in ("Counts", "Search and screening", "Evidence maps", "Missing information", "Modelling readiness",
                    "Comparison sets", "Validation", "Evidence table", "Inputs and audit trail", "Limitations"):
        assert f">{section}</h2>" in page
    assert "hash chain verified" in text


def test_report_numbers_are_the_numbers_of_the_application_functions(corpus):
    conn = corpus.conn
    page = html.render_report(corpus)
    text = _text(page)
    counts = query.counts(conn)
    assert f'<div class="n">{counts["reports"]}</div><div class="l">Reports (papers)</div>' in page
    assert f'<div class="n">{counts["findings"]}</div><div class="l">Findings</div>' in page
    emap = maps.evidence_map(conn, "system_context")
    for stage in emap["rows"]:
        for term in emap["columns"]:
            v = maps.cell(emap, stage["key"], term["key"])
            if v:
                tip = f"{stage['label']} × {term['label']}: {v} findings"
                assert f'title="{html.escape(tip)}"' in page
    miss = missing.missing_information(conn)
    row = next(r for r in miss["rows"] if r["stage_key"] == missing.ALL_STAGES and r["field"] == "outcome")
    assert (f"<td>(all stages)</td><td>outcome</td><td class=num>{row['findings']}</td>"
            f"<td class=num>{row['recorded']}</td>") in page
    ident = screening.summary(conn)["identification"]
    assert f"<td>paper records</td><td class=num>{ident['paper_records']}</td>" in page
    assert f"{counts['findings']} of {counts['findings']} finding(s), ordered by report." in text
    sha = conn.execute("SELECT sha256 FROM workbook_version").fetchone()[0]
    assert sha in page


def test_report_escapes_source_text(imported):
    project, _ = imported
    conn = project.conn
    fid = conn.execute("SELECT id FROM finding ORDER BY pk LIMIT 1").fetchone()[0]
    hostile = '<script>alert("x")</script> & <img src=x onerror=1>'
    review.edit(conn, fid, REVIEWER_A, {"system_context": hostile})
    page = html.render_report(project)
    assert "<script" not in page.lower() and "<img src=x" not in page
    assert html.escape(hostile) in page


def test_report_shows_saved_comparison_sets_validation_and_filter(imported):
    project, _ = imported
    conn = project.conn
    ids = [r[0] for r in conn.execute("SELECT id FROM finding WHERE unit_original = 'mg/L' ORDER BY pk LIMIT 2")]
    for fid in ids:
        review.verify(conn, fid, REVIEWER_A, ALL_CONFIRMED, acknowledge_flags=True)
    saved = comparison.save_set(conn, "Synthetic pair", ids, REVIEWER_A)
    validation.run_validation(project, OPERATOR)
    text = _text(html.render_report(project))
    assert "Synthetic pair" in text and saved["classification"].replace("_", " ") in text
    assert "nothing is pooled, averaged or ranked" in text
    assert "Latest run" in text and "finding(s) checked" in text
    narrowed = _text(html.render_report(project, flt=query.FindingFilter(stage_keys=["release"])))
    assert "Filtered report." in narrowed and "matching the filter" in narrowed
    n = query.search_findings(conn, query.FindingFilter(stage_keys=["release"]), limit=1).total
    assert f"{n} of {n} finding(s) matching the filter" in narrowed


def test_report_limits_the_evidence_table_and_says_so(imported):
    project, _ = imported
    text = _text(html.render_report(project, max_findings=5))
    assert "5 of 32 finding(s), ordered by report. The complete table is in the export files" in text


def test_build_report_writes_html_figures_tables_and_manifest(corpus, tmp_path):
    out = build.build_report(corpus, tmp_path / "report", actor=OPERATOR)
    names = {p.name for p in out["files"]}
    assert all(n.startswith("SYNTHETIC_") for n in names)
    assert "SYNTHETIC_report.html" in names and out["html"].name == "SYNTHETIC_report.html"
    for fig in ("evidence_map_system_context", "evidence_map_outcome", "missing_information", "search_screening_flow"):
        for ext in ("svg", "pdf", "png"):
            assert f"SYNTHETIC_figure_{fig}.{ext}" in names
    for table in ("findings", "papers", "documents", "search_runs", "screening_decisions", "evidence_map_outcome",
                  "missing_information", "modeling_inventory", "search_screening_flow", "validation_issues",
                  "comparison_sets", "derived_values"):
        assert f"SYNTHETIC_{table}.csv" in names
    assert {"SYNTHETIC_evidence.xlsx", "SYNTHETIC_evidence.json", "SYNTHETIC_references.ris"} <= names
    manifest = json.loads(out["manifest"].read_text(encoding="utf-8"))
    assert manifest["kind"] == "report" and manifest["project"]["data_class"] == "synthetic"
    assert manifest["figure_resolution_dpi"] == 300 and manifest["label"].startswith(BANNER)
    assert {e["name"] for e in manifest["files"]} == names
    for entry in manifest["files"]:
        assert hashlib.sha256((tmp_path / "report" / entry["name"]).read_bytes()).hexdigest() == entry["sha256"]
    # the table of the map holds what the map function returns
    emap = maps.evidence_map(corpus.conn, "outcome")
    with open(tmp_path / "report" / "SYNTHETIC_evidence_map_outcome.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == len(maps.map_table(emap).rows)
    assert sum(int(r["findings"]) for r in rows) == sum(m["findings"] for c in emap["cells"].values() for m in c.values())
    event = corpus.conn.execute("SELECT actor, details_json FROM audit_event WHERE action = 'report.created'").fetchone()
    assert event["actor"] == OPERATOR.name and "SYNTHETIC_report.html" in event["details_json"]


def test_build_report_parts_and_refusals(imported, real_project, tmp_path):
    project, _ = imported
    only = build.build_report(project, tmp_path / "figs", parts=["figures"], figure_formats=["svg"])
    assert sorted(p.suffix for p in only["files"]) == [".svg"] * 4 and only["html"] is None
    with pytest.raises(GeodesicError, match="Report parts"):
        build.build_report(project, tmp_path / "x", parts=["slides"])
    with pytest.raises(GeodesicError, match="figure format"):
        build.build_report(project, tmp_path / "y", parts=["figures"], figure_formats=["gif"])
    # a real project: no synthetic prefix or label, and never written into a source-controlled directory
    real = build.build_report(real_project, tmp_path / "real_report", parts=["html", "figures"], figure_formats=["svg"])
    assert not any(p.name.startswith("SYNTHETIC_") for p in real["files"])
    assert BANNER not in real["html"].read_text(encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(GeodesicError, match="source-controlled"):
        build.build_report(real_project, repo / "report")


def test_export_holds_workflow_and_summary_tables(evidence, tmp_path):
    project, conn = evidence.project, evidence.conn
    names = [t.name for t in T.standard_tables(project)]
    assert names[:7] == ["papers", "findings", "source_locations", "stages", "finding_relations", "unparsed_content",
                         "workbook_versions"]
    assert names[7:] == ["documents", "search_runs", "screening_decisions", "paper_assessments", "studies",
                         "experiments", "derived_values", "vocabulary_terms", "validation_issues", "comparison_sets",
                         "evidence_map_system_context", "evidence_map_outcome", "missing_information",
                         "modeling_inventory", "search_screening_flow"]
    assert all(len(n) <= 31 for n in names)                         # Excel sheet-name limit
    out = export_tables(project, tmp_path / "out", formats=("csv", "xlsx"), actor=OPERATOR)
    assert out["rows"]["documents"] == 2 and out["rows"]["search_runs"] == 1
    with open(tmp_path / "out" / "SYNTHETIC_documents.csv", newline="", encoding="utf-8") as fh:
        docs = list(csv.DictReader(fh))
    assert {d["format"] for d in docs} == {"pdf", "xml"} and all(len(d["sha256"]) == 64 for d in docs)
    assert {d["sha256"] for d in docs} == {r[0] for r in conn.execute("SELECT sha256 FROM document")}
    with open(tmp_path / "out" / "SYNTHETIC_search_runs.csv", newline="", encoding="utf-8") as fh:
        runs = list(csv.DictReader(fh))
    assert runs[0]["mode"] == "fixture" and "client_ident" not in runs[0]     # contact details are not exported
