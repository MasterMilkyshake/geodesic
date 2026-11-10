"""Command line for comparison, vocabulary, evidence maps, summaries, figures and reports.
Every command goes through ``geodesic.cli.main``; all inputs are synthetic."""

from __future__ import annotations

import csv
import json

import pytest

from geodesic.cli import main
from geodesic.cli_reports import dashboard_command

from test_cli_m3 import Workspace


@pytest.fixture
def ws(tmp_path, capsys):
    w = Workspace(tmp_path, capsys)
    assert main(["init", "--project", w.proj, "--config", str(w.cfg), "--data-class", "synthetic"]) == 0
    w.run("import-workbook", w.workbook, "--mapping", w.mapping)
    w.run("search", "run", "--fixture-dir", w.fixture_dir, "--max-records", "50")
    return w


def _ids(ws, where, n):
    import sqlite3

    conn = sqlite3.connect(ws.proj + "/geodesic.sqlite")
    try:
        return [r[0] for r in conn.execute(f"SELECT id FROM finding WHERE {where} ORDER BY pk LIMIT {n}")]
    finally:
        conn.close()


def test_compare_commands(ws, tmp_path):
    ids = _ids(ws, "unit_original = 'mg/L' AND statement_type = 'exact'", 2)
    assert len(ids) == 2
    out = ws.ro("compare", "assess", *ids)
    assert out.startswith("[SYNTHETIC project") and "Classification: descriptive_only" in out
    assert "verification" in out and "are not verified against their source by a reviewer" in out
    assert "values are never pooled or averaged here" in out
    data = json.loads(ws.ro("compare", "assess", *ids, "--json"))
    assert data["classification"] == "descriptive_only" and "verification" in data["blocking"]
    ws.ro("compare", "assess", expect=2)                                   # neither IDs nor a filter
    assert "Name the findings by ID" in ws.err

    out = ws.ro("compare", "table", *ids)
    assert all(i in out for i in ids) and "as stated:" in out and "(identical_unit)" in out
    assert "nothing is pooled, averaged or ranked" in out
    dest = tmp_path / "cmp.csv"
    assert "SYNTHETIC_cmp.csv" in ws.ro("compare", "table", *ids, "--csv", dest)      # labelled in the file name
    with open(tmp_path / "SYNTHETIC_cmp.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["finding_id"] for r in rows] == ids and rows[0]["display_rule"] == "identical_unit"
    assert {r["data_class"] for r in rows} == {"synthetic"} and not dest.exists()

    out = ws.ro("compare", "units", "--stage", "release")
    assert "11 finding(s) form at most 11 independent unit(s)" in out and "upper bound" in out

    # a saved set needs a named reviewer; an unverified set cannot be a quantitative candidate
    ws.ro("compare", "save", "Synthetic pair", *ids, expect=2)
    assert "reviewer" in ws.err.lower()
    ws.run("compare", "save", "Synthetic pair", *ids, "--classification", "quantitative_candidate", expect=2)
    assert "cannot be classified as a quantitative candidate" in ws.err
    out = ws.run("compare", "save", "Synthetic pair", *ids, "--description", "synthetic test set")
    assert "as descriptive_only (2 findings). Nothing was pooled." in out
    set_id = out.split()[3]
    out = ws.ro("compare", "list")
    assert "1 comparison set(s)" in out and set_id in out and "by Synthetic Reviewer A" in out
    out = ws.ro("compare", "show", set_id)
    assert "Assessment as stored:" in out and "synthetic test set" in out and ids[0] in out
    ws.ro("compare", "show", "cmp_missing", expect=2)

    ws.run("compare", "eligibility", "quantitative_candidate", ids[0], expect=2)      # not verified
    assert "cannot be marked quantitative candidates" in ws.err
    assert "1 finding(s) marked descriptive_only" in ws.run("compare", "eligibility", "descriptive_only", ids[0],
                                                           "--note", "synthetic note")
    assert ws.scalar("SELECT comparison_eligibility FROM finding WHERE id = ?", ids[0]) == "descriptive_only"
    assert ws.scalar("SELECT COUNT(*) FROM audit_event WHERE action = 'finding.comparison_eligibility'") == 1

    # an edit after saving is reported, the stored assessment is not rewritten
    ws.run("review", "edit", ids[0], "--set", "outcome=peak concentration")
    out = ws.ro("compare", "list")
    assert "1 finding(s) changed since the assessment" in out
    assert "Assess the set again" in ws.ro("compare", "show", set_id)


def test_vocabulary_commands_and_map(ws):
    a, b = _ids(ws, "system_context IS NOT NULL", 40)[0], None
    terms = json.loads(ws.ro("vocabulary", "usage", "system_context", "--json"))
    assert len(terms) >= 2 and all(t["normalized_term"] is None for t in terms)
    first, second = terms[0]["original_term"], terms[1]["original_term"]
    before = json.loads(ws.ro("map", "--by", "system_context", "--json"))
    labels = [c["label"] for c in before["columns"]]
    assert first in labels and second in labels

    ws.ro("vocabulary", "set", "system_context", first, "Synthetic group", expect=2)        # no reviewer
    ws.run("vocabulary", "set", "system_context", "no such term", "Synthetic group", expect=2)
    assert "must name a term exactly as it is stored" in ws.err
    out = ws.run("vocabulary", "set", "system_context", first, "Synthetic group", "--note", "synthetic mapping")
    assert "is now grouped under 'Synthetic group'" in out and "stored wording is unchanged" in out
    ws.run("vocabulary", "set", "system_context", second, "Synthetic group")
    out = ws.ro("vocabulary", "list")
    assert "2 term mapping(s)" in out and f"{first!r} -> 'Synthetic group'" in out
    assert "-> grouped under 'Synthetic group'" in ws.ro("vocabulary", "usage", "system_context")

    after = json.loads(ws.ro("map", "--by", "system_context", "--json"))
    col = next(c for c in after["columns"] if c["label"] == "Synthetic group")
    assert col["kind"] == "mapped" and sorted(col["original_terms"]) == sorted([first, second])
    assert col["findings"] == terms[0]["findings"] + terms[1]["findings"]
    assert after["total"] == before["total"]                               # grouping changes columns, not counts
    text = ws.ro("map", "--by", "system_context")
    assert "'Synthetic group' groups (reviewer mapping):" in text and "all stages" in text
    # the stored terms are untouched
    assert ws.scalar("SELECT COUNT(*) FROM finding WHERE system_context = ?", first) == terms[0]["findings"]

    ws.run("vocabulary", "remove", "system_context", second, "--reason", "synthetic: grouped by mistake")
    assert "1 term mapping(s)" in ws.ro("vocabulary", "list")
    assert "[withdrawn]" in ws.ro("vocabulary", "list", "--all")
    assert a and b is None


def test_map_missing_and_modeling_commands(ws):
    out = ws.ro("map", "--by", "outcome", "--metric", "reports")
    assert "Evidence map: stage by outcome — reports (papers) the findings come from" in out
    assert "(not recorded)" in out and "it does not mean that no evidence exists" in out
    release = json.loads(ws.ro("map", "--stage", "release", "--json"))
    assert release["total"]["findings"] == 11
    with pytest.raises(SystemExit):                                        # argparse rejects an unknown field
        ws.ro("map", "--by", "colour")

    out = ws.ro("missing")
    assert "Missing-information summary (measurement findings)" in out and "(all stages)" in out
    assert "'not_reported' means someone recorded that the source does not state it" in out
    assert "Full-text availability (18 reports; 8 with findings)" in out and "not checked: 17" in out
    only = ws.ro("missing", "--only-stage", "release")
    assert "Release" in only and "Transit\n" not in only
    data = json.loads(ws.ro("missing", "--json"))
    assert data["missing_information"]["measurement_findings"] == 20 and data["fulltext"]["reports"] == 18

    out = ws.ro("modeling")
    assert "No model is fitted, no values are pooled" in out and "(all stages): quantitative findings 20" in out
    assert "Reviewer assessments, mathematical modeling: none recorded" in out


def test_figures_report_and_filtered_export_commands(ws, tmp_path):
    out = ws.run("figures", "--formats", "svg,png", "--out", tmp_path / "figs", reviewer=None)
    assert out.startswith("[SYNTHETIC project")
    names = sorted(p.name for p in (tmp_path / "figs").iterdir())
    assert len(names) == 9 and "SYNTHETIC_manifest.json" in names
    assert "SYNTHETIC_figure_search_screening_flow.png" in names
    ws.ro("figures", "--formats", "png", "--dpi", "150", "--out", tmp_path / "low", expect=2)
    assert "300 DPI" in ws.err
    ws.ro("figures", "--formats", "bmp", "--out", tmp_path / "bad", expect=2)

    out = ws.run("report", "--out", tmp_path / "report", "--title", "Synthetic walk-through")
    assert "Report directory:" in out and "one self-contained file" in out
    page = (tmp_path / "report" / "SYNTHETIC_report.html").read_text(encoding="utf-8")
    assert "Synthetic walk-through" in page and "SYNTHETIC DATA" in page and "<script" not in page.lower()
    manifest = json.loads((tmp_path / "report" / "SYNTHETIC_manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "report" and manifest["rows"]["findings"] == 32
    assert ws.scalar("SELECT actor FROM audit_event WHERE action = 'report.created' ORDER BY seq DESC LIMIT 1") \
        == "Synthetic Reviewer A"
    ws.ro("report", "--parts", "slides", "--out", tmp_path / "x", expect=2)

    out = ws.ro("report", "--parts", "html", "--stage", "release", "--out", tmp_path / "filtered")
    assert [p.name for p in (tmp_path / "filtered").iterdir() if "report" in p.name] == ["SYNTHETIC_report.html"]
    assert "Filtered report." in (tmp_path / "filtered" / "SYNTHETIC_report.html").read_text(encoding="utf-8")

    out = ws.ro("export", "--stage", "release", "--formats", "csv", "--out", tmp_path / "export")
    assert "findings: 11" in out
    ws.ro("export", "--formats", "docx", "--out", tmp_path / "bad_export", expect=2)


def test_dashboard_command_is_local_and_names_the_project(tmp_path):
    cmd = dashboard_command(tmp_path / "proj", port=8765, headless=True)
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("dashboard/app.py")
    assert cmd[cmd.index("--server.address") + 1] == "localhost"
    assert cmd[cmd.index("--browser.gatherUsageStats") + 1] == "false"
    assert cmd[-3:] == ["--", "--project", str(tmp_path / "proj")]
    assert main(["dashboard", "--project", str(tmp_path / "missing")]) == 2
