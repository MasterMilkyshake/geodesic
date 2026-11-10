"""Command line for documents, candidate values, manual extraction, review, hierarchy, validation and
normalized/derived values. Every command goes through ``geodesic.cli.main``; all inputs are synthetic."""

from __future__ import annotations

import json
import sqlite3

import pytest

from geodesic.cli import main
from geodesic.config import dump_yaml
from geodesic.synthetic import documents as SD, search as SS
from geodesic.synthetic.workbook import example_config, fixture_mapping, write_fixture_workbook
from geodesic.workbook.mapping import write_mapping

TANK = "The source tank held 250 L of water and was stirred at 120 rpm."


class Workspace:
    def __init__(self, root, capsys):
        self.root, self.capsys = root, capsys
        self.proj = str(root / "proj")
        self.cfg = root / "project.yaml"
        dump_yaml(example_config().model_dump(mode="json"), self.cfg)
        self.workbook = write_fixture_workbook(root / "wb" / "w.xlsx", version=1)
        self.mapping = root / "mapping.yaml"
        write_mapping(fixture_mapping(confirmed=True), self.mapping)
        self.fixture_dir = SS.write_europepmc_fixture(root / "fx1", page_size=4, variant=1)
        self.pdf = SD.write_pdf_fixture(root / "docs" / "main.pdf")
        self.scan = SD.write_scanned_pdf_fixture(root / "docs" / "scan.pdf")
        self.sup = SD.write_supplement_pdf_fixture(root / "docs" / "sup.pdf")
        self.untagged = SD.write_untagged_pdf_fixture(root / "docs" / "untagged.pdf")

    def run(self, *args, reviewer="Synthetic Reviewer A", expect=0):
        """Run one command; return its stdout. ``reviewer=None`` for read-only commands."""
        self.capsys.readouterr()
        argv = [*map(str, args), "--project", self.proj]
        if reviewer:
            argv += ["--reviewer", reviewer]
        code = main(argv)
        out = self.capsys.readouterr()
        assert code == expect, f"exit {code}, expected {expect}\n{out.out}\n{out.err}"
        self.err = out.err
        return out.out

    def ro(self, *args, expect=0):
        return self.run(*args, reviewer=None, expect=expect)

    def scalar(self, sql, *params):
        conn = sqlite3.connect(self.proj + "/geodesic.sqlite")
        try:
            return conn.execute(sql, params).fetchone()[0]
        finally:
            conn.close()


@pytest.fixture
def ws(tmp_path, capsys):
    w = Workspace(tmp_path, capsys)
    assert main(["init", "--project", w.proj, "--config", str(w.cfg), "--data-class", "synthetic"]) == 0
    w.run("import-workbook", w.workbook, "--mapping", w.mapping)
    w.run("search", "run", "--fixture-dir", w.fixture_dir, "--max-records", "50")
    w.wp = w.scalar("SELECT id FROM paper WHERE doi = ? AND origin = 'workbook'", SD.PDF_DOI)
    w.xp = w.scalar("SELECT id FROM paper WHERE pmcid = ?", SD.XML_PMCID)
    w.other = w.scalar("SELECT id FROM paper WHERE origin = 'workbook' AND doi IS NOT NULL AND doi != ? ORDER BY pk",
                       SD.PDF_DOI)
    return w


@pytest.fixture
def docs_ws(ws):
    """Workspace with the PDF attached, the open-access XML retrieved from the fixture and candidates detected."""
    ws.run("documents", "add", ws.wp, ws.pdf)
    ws.run("documents", "fetch-oa", ws.xp, "--synthetic-fixture")
    ws.doc = ws.scalar("SELECT id FROM document WHERE paper_id = ?", ws.wp)
    ws.xdoc = ws.scalar("SELECT id FROM document WHERE paper_id = ?", ws.xp)
    ws.run("candidates", "detect", ws.doc)
    ws.run("candidates", "detect", ws.xdoc)
    return ws


# ------------------------------------------------------------------ documents
def test_documents_commands(ws):
    out = ws.ro("documents", "identify", ws.pdf)
    assert "parse status ok" in out and f"matches paper {ws.wp}" in out and f"doi:{SD.PDF_DOI}" in out

    ws.run("documents", "add", ws.other, ws.pdf, expect=2)                 # identifiers belong to another paper
    assert "do not match paper" in ws.err and "--allow-identifier-mismatch" in ws.err
    ws.run("documents", "add", ws.other, ws.untagged, expect=2)            # no provenance tag, none declared
    assert "--declare synthetic or --declare real" in ws.err
    ws.run("documents", "add", ws.other, ws.untagged, "--declare", "real", expect=2)
    assert ws.scalar("SELECT COUNT(*) FROM document") == 0

    out = ws.run("documents", "add", ws.wp, ws.pdf)
    assert out.startswith("[SYNTHETIC project") and "[declared synthetic]" in out
    assert "status ok, 3 text unit(s), 814 characters, 0 table(s)" in out
    assert "identifier check: match (doi)" in out and "license: not supplied" in out
    assert "full-text availability of the paper: document_attached" in out
    assert "Nothing was added." in ws.run("documents", "add", ws.wp, ws.pdf)
    assert "(pdf, supplementary)" in ws.run("documents", "add", ws.wp, ws.sup, "--role", "supplementary")

    out = ws.run("documents", "add", ws.other, ws.scan)
    assert "status image_only" in out and "OCR is not implemented" in out
    assert "full-text availability of the paper: not_checked" in out
    assert "1 attached document(s) without usable text" in ws.ro("documents", "status", ws.other)
    ws.run("documents", "unavailable", ws.other, "--status", "not_available", "--note", "", expect=2)
    out = ws.run("documents", "unavailable", ws.other, "--status", "not_available",
                 "--note", "synthetic note: looked in the fixture set")
    assert "full text recorded as not_available" in out
    assert "a document with extractable text is attached" in ws.ro("documents", "status", ws.wp)
    assert ws.ro("documents", "list").count("\ndoc_") == 3

    doc = ws.scalar("SELECT id FROM document WHERE paper_id = ? AND role = 'main'", ws.wp)
    out = ws.ro("documents", "show", doc)
    assert "OCR applied: no" in out and "0 | page 1 |  | 367" in out
    out = ws.ro("documents", "show", doc, "--unit", "1")
    assert "[page 2]" in out and "The outflow concentration peaked at 18.5 mg/L." in out
    out = ws.ro("documents", "find", doc, "18.5 mg/L")
    assert "1 occurrence(s)" in out and "page index 1 chars 297-306" in out
    out = ws.run("documents", "reparse", doc)
    assert "(no. 2): status ok; text identical to the previous snapshot: True" in out
    out = ws.run("documents", "remove", doc, "--reason", "synthetic removal check")
    assert "marked removed (nothing deleted)" in out
    assert ws.scalar("SELECT record_status FROM document WHERE id = ?", doc) == "removed"
    data = json.loads(ws.ro("documents", "list", "--json"))
    assert isinstance(data, (list, dict))


def test_open_access_retrieval_command(ws):
    out = ws.run("documents", "fetch-oa", "--candidates")
    assert "1 paper(s) reported open access with a PMCID" in out and ws.xp in out
    ws.run("documents", "fetch-oa", ws.xp, expect=2)                       # live retrieval in a synthetic project
    assert "not permitted in a synthetic project" in ws.err
    ws.run("documents", "fetch-oa", ws.wp, "--synthetic-fixture", expect=2)
    assert "limited to records reported as open access" in ws.err
    out = ws.run("documents", "fetch-oa", ws.xp, "--synthetic-fixture")
    assert out.startswith("[SYNTHETIC fixture response] attached: document doc_")
    assert f"/rest/{SD.XML_PMCID}/fullTextXML" in out and f"license: {SD.XML_LICENSE_URL}" in out
    assert "12 text unit(s), 1 table(s)" in out
    assert ws.scalar("SELECT source FROM document") == "open_access_api:europepmc"
    xdoc = ws.scalar("SELECT id FROM document")
    out = ws.ro("documents", "show", xdoc, "--tables")
    assert "body[1]/sec[1]/sec[1]/p[1] | p | Methods > Tracer injection | 94" in out
    assert "Table 1 | 4 rows x 3 columns" in out and "r2 c1: 23 ± 2" in out and "r0 c1 (header): Transit time (min)" in out


# ------------------------------------------------------------------ candidates and manual extraction
def test_candidate_commands(docs_ws):
    ws = docs_ws
    assert "candidates were already detected (11)" in ws.run("candidates", "detect", ws.doc)
    out = ws.ro("candidates", "list", "--snapshot", ws.doc, "--with-unit")
    assert "7 candidate(s) shown; totals {'open': 11, 'promoted': 0, 'dismissed': 0}" in out
    assert "'62 ± 5 %'" in out and "flags: percent_reference_needs_review, uncertainty_type_unspecified" in out
    cid = ws.scalar("SELECT id FROM numeric_candidate WHERE literal = '18.5 mg/L'")
    out = ws.ro("candidates", "show", cid)
    assert "draft reading (not stored): exact 18.5 mg/L" in out and "page index 1 chars 297-306" in out

    out = ws.run("candidates", "promote", cid, "--set", "outcome=outflow concentration (peak)", "--stage", "discharge",
                 "--status-of", "sample_size=not_reported")
    assert "finding created from the candidate (not yet verified)" in out and "[unreviewed, grounded]" in out
    assert "sample_size: not_reported" in out and "page index 1 chars 261-307" in out
    ws.run("candidates", "promote", cid, expect=2)
    assert "already 'promoted'" in ws.err

    conn = sqlite3.connect(ws.proj + "/geodesic.sqlite")
    bare = [r[0] for r in conn.execute("SELECT id FROM numeric_candidate WHERE location_kind = 'pdf_page' AND"
                                       " json_extract(parsed_json, '$.has_unit') = 0")]
    conn.close()
    assert "4 candidate(s) dismissed (kept in the record)" in ws.run("candidates", "dismiss", *bare, "--note", "section numbers")
    cell = ws.scalar("SELECT id FROM numeric_candidate WHERE literal = '23 ± 2'")
    out = ws.run("candidates", "promote", cell, "--stage", "transit")
    assert "unit_original='min'" in out and "unit_from_column_header" in out
    assert "source [context:unit_original]:" in out and "row 0 column 1 chars 0-18" in out
    assert ws.scalar("SELECT COUNT(*) FROM numeric_candidate WHERE status = 'open'") == 27 - 6
    assert ws.scalar("SELECT COUNT(*) FROM finding WHERE review_status = 'verified'") == 0


def test_manual_extraction_commands(docs_ws):
    ws = docs_ws
    where = ("--snapshot", ws.doc, "--unit", "0", "--quote", TANK)
    n = ws.scalar("SELECT COUNT(*) FROM finding")
    out = ws.ro("extract", "propose", *where)
    assert "page index 0 chars 236-299" in out and "nothing is stored" in out
    assert "[1] exact 250 L" in out and "spans: value_literal=257:260, unit_original=261:262" in out
    assert ws.scalar("SELECT COUNT(*) FROM finding") == n

    out = ws.run("extract", "add", ws.wp, *where, "--quantity", "1", "--set", "outcome=source tank volume",
                 "--stage", "release")
    assert "[draft, grounded]" in out and "origin: manual_extraction" in out and "multiple_quantities_in_segment" in out

    # a transcription that is not in the span is refused (exit 2) and logged, never stored
    ws.run("extract", "add", ws.wp, *where, "--set", "value_literal=25", "--set", "value_num=25",
           "--set", "statement_type=exact", expect=2)
    assert "refused and logged as a parse failure" in ws.err and "inside a longer number" in ws.err
    ws.run("extract", "add", ws.wp, *where, "--set", "value_literal=260", "--set", "value_num=260",
           "--set", "statement_type=exact", expect=2)
    assert "claimed source string is absent from the specified span" in ws.err
    assert ws.scalar("SELECT COUNT(*) FROM parse_failure WHERE context = 'manual_extraction'") == 2
    assert ws.scalar("SELECT COUNT(*) FROM finding") == n + 1
    ws.run("extract", "add", ws.wp, *where, "--set", "not_a_field=1", expect=2)

    out = ws.run("extract", "add", ws.wp, "--snapshot", ws.doc, "--unit", "2", "--start", "0", "--end", "13",
                 "--kind", "narrative")
    assert "narrative/qualitative" in out and "wording: 3 Limitations" in out


# ------------------------------------------------------------------ review, derived values, hierarchy, validation
def test_review_derived_hierarchy_and_validation_commands(docs_ws):
    ws = docs_ws
    cid = ws.scalar("SELECT id FROM numeric_candidate WHERE literal = '18.5 mg/L'")
    ws.run("candidates", "promote", cid, "--set", "outcome=outflow concentration", "--stage", "discharge")
    ws.run("extract", "add", ws.wp, "--snapshot", ws.doc, "--unit", "0", "--quote", TANK, "--quantity", "1",
           "--set", "outcome=source tank volume")
    f1 = ws.scalar("SELECT id FROM finding WHERE origin = 'candidate_promotion'")
    f2 = ws.scalar("SELECT id FROM finding WHERE origin = 'manual_extraction'")

    assert "No validation run has been made" in ws.ro("validate", "show")
    ws.run("review", "verify", f1, expect=2)
    assert "Verification needs each of these checked" in ws.err
    ws.run("review", "verify", f2, "--all-confirmed", expect=2)             # an ambiguous draft needs acknowledgement
    out = ws.run("review", "verify", f1, "--all-confirmed", "--note", "checked against page 2",
                 reviewer="Synthetic Reviewer B")
    assert f"finding {f1} version 1 verified against its source by Synthetic Reviewer B" in out
    assert "comment recorded" in ws.run("review", "comment", f2, "--note", "synthetic comment")

    out = ws.run("derived", "normalize", f1, "--to", "µg/mL")
    assert "normalized  18.5 µg/mL  rule unit_conversion:mg/L->µg/mL" in out
    assert "The transcribed value and unit of the finding are unchanged." in out
    ws.run("derived", "normalize", f1, "--to", "L", expect=2)
    assert "different dimensions" in ws.err
    out = ws.run("derived", "compute", "ratio", f2, f1)
    assert "derived  13.5135 (L)/(mg/L)  rule ratio" in out and "'uncertainty': 'not propagated'" in out
    dv = ws.scalar("SELECT id FROM derived_value WHERE kind = 'normalized'")
    out = ws.ro("derived", "trace", dv)
    assert "traceable to stored source text: yes; all inputs at their current version: yes" in out
    assert "page index 1 chars 261-307" in out and "stored text at the literal's span (chars 297-301): '18.5'" in out

    out = ws.run("review", "edit", f1, "--set", "outcome=peak outflow concentration")
    assert "is now version 2 (grounded); changed: outcome. Earlier verification no longer applies." in out
    assert "0 value(s)" in ws.ro("derived", "list")
    assert ws.ro("derived", "list", "--all").count("[STALE: finding edited by reviewer]") == 2
    out = ws.ro("review", "show", f1)
    assert "version 2  review_edit" in out and "review  verify  version 1  by Synthetic Reviewer B" in out
    assert "[unreviewed, grounded]" in out
    assert "stages: transit, discharge" in ws.run("review", "stages", f1, "--add", "transit")
    assert f"{f1} dependent {f2}" in ws.run("review", "relate", f1, f2, "--type", "dependent", "--note", "same run")

    ws.run("hierarchy", "study-add", "Synthetic tank study", "--papers", ws.wp)
    study = ws.scalar("SELECT id FROM study")
    ws.run("hierarchy", "experiment-add", ws.wp, "Run 1", "--study", study)
    exp = ws.scalar("SELECT id FROM experiment")
    ws.run("hierarchy", "group-add", exp, "High flow", "--role", "treatment")
    grp = ws.scalar("SELECT id FROM comparison_group")
    out = ws.run("hierarchy", "assign", f1, "--group", grp)
    assert f"study {study}, experiment {exp}, group {grp}" in out
    out = ws.ro("hierarchy", "tree")
    assert "Synthetic tank study" in out and "High flow  role: treatment" in out
    assert "findings not assigned to any study, experiment or group: 33" in out

    out = ws.run("validate", "run", "--strict")                            # warnings only: exit 0
    assert "does not verify that findings represent their sources correctly" in out
    assert "0 finding(s) with errors" in out and "derived_value_stale: 2" in out
    out = ws.ro("validate", "show", "--severity", "warning", "--limit", "5")
    assert f"[warning] {f1}  derived_value_stale" in out

    assert "withdrawn (kept in the record)" in ws.run("derived", "withdraw", dv, "--reason", "synthetic check")
    assert "rejected (kept in the record)" in ws.run("review", "reject", f2, "--note", "synthetic rejection")
    assert "reopened for review" in ws.run("review", "reopen", f2)
    out = ws.run("documents", "remove", ws.doc, "--reason", "synthetic removal check")
    assert "2 finding(s) cite it" in out
    out = ws.run("validate", "run", "--strict", expect=4)                   # errors: non-zero exit
    assert "source_document_removed: 2" in out and "2 finding(s) with errors" in out
    assert "hash chain intact" in ws.run("audit", "verify")


def test_unit_table_command_needs_no_project(capsys):
    assert main(["derived", "units"]) == 0
    out = capsys.readouterr().out
    assert "mg/L         mass_concentration     base g/L      factor 0.001" in out
    assert "[ambiguous: 'm' may also be written for minutes or months]" in out
    assert main(["derived", "units", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(r["symbol"] == "%" and r["factor_to_base"] is None for r in rows)
