"""Search-result file import (RIS, CSV): parsing as documented, preservation, idempotence, provenance."""

from __future__ import annotations

import pytest

from conftest import OPERATOR
from geodesic.errors import GeodesicError, ProvenanceError
from geodesic.provenance import TAG_TEXT
from geodesic.search import fileimport, snapshot
from geodesic.synthetic import search as SS
from geodesic.util import sha256_file


def test_ris_is_parsed_as_documented():
    records, notes = fileimport.parse_ris(SS.RIS_TEXT)
    parsed = [r for _raw, r in records]
    assert [r.title for r in parsed] == [
        "Dye pulse dispersion in a baffled conduit",
        "Holding reservoir retention of an inert tracer: a conference summary",
        "Outflow tracer sampling at a discharge weir", "A record without the end-of-record tag"]
    first = parsed[0]
    assert first.authors == ["Norcross, V.", "Oakes, W."] and first.year == 2022      # PY 2022/03/14/ -> 2022
    assert first.publication_type == "JOUR"                                           # original code kept
    assert first.doi == "https://doi.org/10.5555/GEODESIC.SYNTH.101"                  # as supplied
    assert first.abstract.endswith("linking two reservoirs. Transit time and dispersion were measured at three"
                                   " flow rates.")                                   # continuation line joined
    assert first.urls == ["https://example.invalid/synthetic/ris/101"] and first.language == "eng"
    assert parsed[1].pmid == "90000042" and parsed[1].journal == "Proceedings of the Synthetic Flow Conference"
    assert parsed[2].authors == ["Yarrow, L."] and parsed[2].year == 2019             # A1 / Y1 alternatives
    assert records[2][0]["ZZ"] == ["an unknown tag is kept with the stored record"]
    assert notes == ["record 4 has no ER tag; it was read up to the next record or end of file"]


def test_csv_is_parsed_as_documented():
    records, notes = fileimport.parse_csv(SS.CSV_TEXT)
    parsed = [r for _raw, r in records]
    assert len(parsed) == 3
    assert parsed[0].authors == ["Underhill, C.", "Vance, D."] and parsed[0].year == 2024
    assert parsed[0].pmid == "90000011" and parsed[0].publication_type == "journal article"
    assert parsed[1].doi == "doi:10.5555/geodesic.synth.101" and parsed[1].abstract is None
    assert parsed[2].title == "Untitled synthetic entry" and parsed[2].year is None    # "n.d." is not a year
    assert records[0][0]["Extra column"] == "kept"
    assert notes == ["columns kept with the stored record but not interpreted: Extra column"]
    skipped = fileimport.parse_csv("title,year\n,2020\nSomething,2021\n")
    assert len(skipped[0]) == 1 and skipped[1] == ["1 row(s) without title, DOI, PMID or PMCID were skipped"]
    with pytest.raises(GeodesicError, match="needs at least one of the columns"):
        fileimport.parse_csv("colour,size\nred,3\n")
    with pytest.raises(GeodesicError, match="no header row"):
        fileimport.parse_csv(f"# {TAG_TEXT}\n")


def test_file_import_preserves_the_file_and_records_a_run(project, tmp_path):
    path = SS.write_ris_fixture(tmp_path / "in" / "export.ris")
    before = sha256_file(path)
    out = fileimport.import_search_file(project, path, OPERATOR, source_label="synthetic reference manager")
    run = out["run"]
    assert sha256_file(path) == before                                   # the input is not modified
    assert run["mode"] == "file_import" and run["provider"] == "file:ris" and run["status"] == "complete"
    assert run["source_file_sha256"] == before and run["source_file_name"] == "export.ris"
    assert run["retrieved_count"] == 4 and run["provider_total"] is None
    assert "completeness relative to the original search is unknown" in run["completeness_note"]
    assert run["filters"] == {"source_label": "synthetic reference manager", "format": "ris"}
    assert "FILE IMPORT" in run["label"] and "of unknown reported" in run["label"]
    stored = project.conn.execute("SELECT stored_path, sha256 FROM search_raw_response WHERE search_run_id = ?",
                                  (run["id"],)).fetchone()
    copy = project.abs(stored["stored_path"])
    assert copy.read_bytes() == path.read_bytes() and stored["sha256"] == before
    assert copy.parent == project.search_files_dir
    assert out["links"] == {"new_paper": 4, "exact_duplicate_record": 0, "validated_identifier_match": 0}
    assert snapshot.verify_snapshot(project, run["id"])["intact"]
    # identifiers read from a file are normalised but stay unvalidated; a malformed one is kept as invalid
    idents = {r["value_norm"] or r["value_raw"]: r["validation_status"] for r in project.conn.execute(
        "SELECT value_norm, value_raw, validation_status FROM paper_identifier WHERE scheme IN ('doi','pmid')")}
    assert idents["10.5555/geodesic.synth.101"] == "unvalidated" and idents["90000042"] == "unvalidated"
    assert "invalid_syntax" in idents.values()
    papers = project.conn.execute("SELECT origin, identity_status FROM paper").fetchall()
    assert {tuple(p) for p in papers} == {("file_import", "unverified")}


def test_importing_the_same_file_again_changes_nothing(project, tmp_path):
    path = SS.write_csv_fixture(tmp_path / "export.csv")
    first = fileimport.import_search_file(project, path, OPERATOR)
    counts = [project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("paper", "search_run", "search_result", "paper_identifier", "audit_event")]
    again = fileimport.import_search_file(project, path, OPERATOR)
    assert again["status"] == "already_imported" and again["run"]["id"] == first["run"]["id"]
    assert counts == [project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("paper", "search_run", "search_result", "paper_identifier", "audit_event")]
    # the same records in another file are recognised record by record
    other = tmp_path / "renamed.csv"
    other.write_text(SS.CSV_TEXT + "Another synthetic title,,2020,,,,,,\n", encoding="utf-8")
    third = fileimport.import_search_file(project, other, OPERATOR)
    assert third["links"] == {"new_paper": 1, "exact_duplicate_record": 3, "validated_identifier_match": 0}


def test_file_provenance_must_agree_with_the_project(project, real_project, tmp_path):
    tagged = SS.write_ris_fixture(tmp_path / "tagged.ris")
    with pytest.raises(ProvenanceError):
        fileimport.import_search_file(real_project, tagged, OPERATOR)
    with pytest.raises(ProvenanceError):
        fileimport.import_search_file(real_project, tagged, OPERATOR, declare="real")
    untagged = tmp_path / "untagged.csv"
    untagged.write_text("title,year\nAn invented title for a provenance test,2020\n", encoding="utf-8")
    with pytest.raises(ProvenanceError):
        fileimport.import_search_file(project, untagged, OPERATOR)                 # declaration required
    with pytest.raises(ProvenanceError):
        fileimport.import_search_file(project, untagged, OPERATOR, declare="real")
    assert fileimport.import_search_file(project, untagged, OPERATOR, declare="synthetic")["links"]["new_paper"] == 1
    assert real_project.conn.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 0


def test_unsupported_or_unreadable_files_are_refused_with_a_reason(project, tmp_path):
    bib = tmp_path / "export.bib"
    bib.write_text(f"% {TAG_TEXT}\n@article{{x, title={{T}}}}\n", encoding="utf-8")
    with pytest.raises(GeodesicError, match="Unsupported search-result file format 'bib'. Supported: ris, csv"):
        fileimport.import_search_file(project, bib, OPERATOR)
    latin = tmp_path / "latin.csv"
    latin.write_bytes(f"# {TAG_TEXT}\ntitle\nCaf\xe9\n".encode("latin-1"))
    with pytest.raises(GeodesicError, match="not UTF-8"):
        fileimport.import_search_file(project, latin, OPERATOR)
    empty = tmp_path / "empty.ris"
    empty.write_text(f"N1  - {TAG_TEXT}\n", encoding="utf-8")
    with pytest.raises(GeodesicError, match="No records could be read"):
        fileimport.import_search_file(project, empty, OPERATOR)
    with pytest.raises(GeodesicError, match="not found"):
        fileimport.import_search_file(project, tmp_path / "missing.ris", OPERATOR)
    assert project.conn.execute("SELECT COUNT(*) FROM search_run").fetchone()[0] == 0
