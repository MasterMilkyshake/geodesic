"""Crossref lookups: identifier validation, enrichment, integrity checks and citation proposals (fixtures only)."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import OPERATOR, REVIEWER_A
from geodesic import audit, papers
from geodesic.errors import ProvenanceError, RetrievalError, ReviewError, ValidationError
from geodesic.search import crossref
from geodesic.synthetic import search as SS
from geodesic.util import Actor, utc_today

DOI = "10.5555/geodesic.synth."


def _paper(project, doi_suffix):
    return project.conn.execute("SELECT id FROM paper WHERE doi = ?", (DOI + doi_suffix,)).fetchone()["id"]


def _doi_status(project, pid, doi_suffix):
    row = project.conn.execute("SELECT validation_status, validated_via, validated_at FROM paper_identifier WHERE"
                               " paper_id = ? AND scheme = 'doi' AND value_norm = ?", (pid, DOI + doi_suffix)).fetchone()
    return tuple(row)


@pytest.fixture
def validated(imported):
    project, _ = imported
    project.crossref_stats = crossref.validate_identifiers(project, OPERATOR, transport=SS.crossref_transport())
    return project


def test_validation_outcomes_and_counts(validated):
    stats = validated.crossref_stats
    assert {k: stats[k] for k in ("looked_up", "validated", "not_found", "possible_misassignment", "no_doi")} == \
        {"looked_up": 6, "validated": 3, "not_found": 2, "possible_misassignment": 1, "no_doi": 0}
    assert stats["errors"] == [] and stats["provider"] == "crossref" and stats["checked_on"] == utc_today()
    # a second pass finds nothing left to look up (malformed and already-checked identifiers are skipped)
    again = crossref.validate_identifiers(validated, OPERATOR, transport=SS.crossref_transport())
    assert again["looked_up"] == 0


def test_validated_identifier_fills_empty_fields_and_records_the_source(validated):
    conn = validated.conn
    pid = _paper(validated, "001")
    status, via, at = _doi_status(validated, pid, "001")
    assert (status, via) == ("validated", "crossref") and at.endswith("+00:00")
    p = papers.get_paper(conn, pid)
    assert p["title"] == "Tracer release from a stirred source tank"
    assert p["authors"] == ["Arden, Q", "Belmont, T"] and p["journal"] == "J Synth Hydraul"
    assert p["identity_status"] == "identifier_validated"
    # the citation as written in the workbook is untouched
    assert p["citation_text"].startswith("Arden Q, Belmont T. Tracer release")
    cand = dict(conn.execute("SELECT * FROM citation_candidate WHERE paper_id = ?", (pid,)).fetchone())
    assert (cand["status"], cand["match_basis"], cand["provider"]) == ("auto_attached", "exact_identifier", "crossref")
    # the stored raw response is the provider's answer, addressed by its hash
    raw = validated.raw_dir / "crossref" / f"{cand['response_sha256']}.json"
    assert raw.is_file() and json.loads(raw.read_bytes())["message"]["DOI"] == DOI + "001"
    event = [e for e in audit.history(conn, entity_type="paper", entity_id=pid)
             if e["action"] == "paper.identifier_lookup"][0]
    details = json.loads(event["details_json"])
    assert details["outcome"] == "validated" and "title" in details["filled"]
    assert details["checked_on"] == utc_today()


def test_not_found_is_recorded_as_not_found_in_that_source(validated):
    conn = validated.conn
    pid = _paper(validated, "007")
    status, via, at = _doi_status(validated, pid, "007")
    assert (status, via) == ("not_found", "crossref") and at
    p = papers.get_paper(conn, pid)
    assert p["title"] is None and p["identity_status"] == "unverified"      # the record is left as it was
    assert p["integrity_checks"] == [] and p["integrity_statement"] == "correction/retraction status not checked"
    cand = dict(conn.execute("SELECT * FROM citation_candidate WHERE paper_id = ?", (pid,)).fetchone())
    assert cand["status"] == "not_found" and cand["response_sha256"] is None


def test_unrelated_registry_title_is_flagged_as_possible_misassignment(validated):
    conn = validated.conn
    pid = _paper(validated, "004")
    assert _doi_status(validated, pid, "004")[0] == "conflict"
    p = papers.get_paper(conn, pid)
    assert p["identity_status"] == "conflict"
    assert p["title"] is None and p["authors"] == []            # nothing was filled from the registry record
    conflict = [c for c in p["metadata_conflicts"] if c["field"] == "title"]
    assert len(conflict) == 1 and conflict[0]["incoming_value"] == "Quarterly maintenance schedule for pump bearings"
    assert conflict[0]["incoming_source"] == "metadata_lookup:crossref" and conflict[0]["status"] == "open"
    cand = dict(conn.execute("SELECT * FROM citation_candidate WHERE paper_id = ?", (pid,)).fetchone())
    assert cand["status"] == "proposed" and cand["decided_by"] is None and cand["score"] == 0.0
    actions = [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=pid)]
    assert "paper.identifier_conflict" in actions
    assert p["integrity_checks"] == []


def test_integrity_checks_name_source_and_date(validated):
    conn = validated.conn
    today = utc_today()
    clean = papers.get_paper(conn, _paper(validated, "001"))
    assert [c["result"] for c in clean["integrity_checks"]] == ["none_found"]
    assert clean["integrity_statement"] == \
        f"no correction or retraction notice found in Crossref (updated-by field of the work record) on {today}"
    assert "not retracted" not in clean["integrity_statement"]
    corrected = papers.get_paper(conn, _paper(validated, "002"))
    check = corrected["integrity_checks"][0]
    assert (check["result"], check["checked_on"], check["notice_url"]) == \
        ("correction", today, "https://doi.org/10.5555/geodesic.synth.902")
    assert check["details"] == "correction: Correction 2021-03-01" and check["recorded_by"] == OPERATOR.name
    assert corrected["integrity_statement"].startswith("correction reported by Crossref")
    assert f"(checked {today})" in corrected["integrity_statement"]


def test_citation_proposals_need_a_reviewer(validated):
    conn = validated.conn
    pid = conn.execute("SELECT id FROM paper WHERE citation_text LIKE 'Hallam J.%'").fetchone()["id"]
    before = papers.get_paper(conn, pid)
    assert before["resolution_status"] == "unresolved" and before["doi"] is None
    proposals = crossref.propose_for_citation(validated, pid, OPERATOR, transport=SS.crossref_transport())
    assert len(proposals) == 3 and proposals[0]["doi"] == DOI + "005" and proposals[0]["title_similarity"] == 1.0
    # proposals alone change nothing on the paper record
    after = papers.get_paper(conn, pid)
    assert (after["doi"], after["title"], after["resolution_status"]) == (None, None, "unresolved")
    assert {r["status"] for r in conn.execute("SELECT status FROM citation_candidate WHERE paper_id = ?", (pid,))} \
        == {"proposed"}
    best, second = proposals[0]["candidate_id"], proposals[1]["candidate_id"]
    with pytest.raises(ReviewError, match="named reviewer"):
        crossref.decide_candidate(conn, best, Actor("geodesic-auto", "automated"), "accepted")
    with pytest.raises(ValidationError):
        crossref.decide_candidate(conn, best, REVIEWER_A, "probably")
    assert crossref.decide_candidate(conn, second, REVIEWER_A, "rejected")["filled"] == []
    out = crossref.decide_candidate(conn, best, REVIEWER_A, "accepted")
    assert "title" in out["filled"] and "doi" in out["filled"]
    p = papers.get_paper(conn, pid)
    assert (p["doi"], p["resolution_status"], p["identity_status"]) == (DOI + "005", "resolved", "reviewer_verified")
    ident = [i for i in p["identifiers"] if i["value_norm"] == DOI + "005"][0]
    assert (ident["validation_status"], ident["validated_via"]) == ("validated", "crossref+reviewer")
    # the malformed identifier from the workbook is still on record
    assert any(i["validation_status"] == "invalid_syntax" for i in p["identifiers"])
    with pytest.raises(ReviewError, match="already"):
        crossref.decide_candidate(conn, best, REVIEWER_A, "rejected")
    event = [e for e in audit.history(conn, entity_type="paper", entity_id=pid)
             if e["action"] == "paper.citation_candidate"][0]
    assert event["actor"] == REVIEWER_A.name and json.loads(event["details_json"])["status"] == "accepted"


def test_lookup_errors_are_reported_and_change_nothing(imported):
    project, _ = imported

    def handler(request):
        return httpx.Response(503, text="unavailable")

    before = project.conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    stats = crossref.validate_identifiers(project, OPERATOR, transport=httpx.MockTransport(handler),
                                          sleep=lambda s: None, limit=2)
    assert stats["validated"] == stats["not_found"] == stats["looked_up"] == 0
    assert len(stats["errors"]) == 2 and "could not be retrieved" in stats["errors"][0]["message"]
    assert project.conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == before
    assert project.conn.execute("SELECT COUNT(*) FROM paper_identifier WHERE validation_status = 'unvalidated'"
                                " AND validated_via IS NOT NULL").fetchone()[0] == 0


def test_response_in_an_unexpected_shape_is_an_error_not_a_validation(imported):
    project, _ = imported
    body = json.dumps({"geodesicFixture": "geodesic-data-class: synthetic", "status": "ok"}).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    stats = crossref.validate_identifiers(project, OPERATOR, transport=transport, limit=1)
    assert stats["validated"] == 0 and "documented response shape" in stats["errors"][0]["message"]


def test_live_lookup_is_refused_in_a_synthetic_project(imported):
    project, _ = imported
    with pytest.raises(ProvenanceError):
        crossref.validate_identifiers(project, OPERATOR)             # no transport = live
    with pytest.raises(ProvenanceError):
        crossref.validate_identifiers(project, OPERATOR, transport=SS.crossref_transport(), live=True)
    with pytest.raises(ProvenanceError):
        crossref.propose_for_citation(project, _paper(project, "001"), OPERATOR)


def test_untagged_fixture_response_is_rejected(imported, real_project):
    project, _ = imported
    untagged = json.dumps({"status": "ok", "message": SS.crossref_works()[DOI + "001"]}).encode()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=untagged))
    with pytest.raises(ProvenanceError):
        crossref.validate_identifiers(project, OPERATOR, transport=transport, limit=1)
    assert _doi_status(project, _paper(project, "001"), "001")[0] == "unvalidated"
    # and tagged synthetic responses cannot be attached to a real project
    conn = real_project.conn
    pid = papers.create_paper(conn, origin="manual", resolution_status="resolved", doi=DOI + "001",
                              citation_text="Generated test citation")
    papers.add_identifier(conn, pid, "doi", DOI + "001", "manual")
    with pytest.raises(ProvenanceError):
        crossref.validate_identifiers(real_project, OPERATOR, transport=SS.crossref_transport())
    assert _doi_status(real_project, pid, "001")[0] == "unvalidated"
    assert not (real_project.raw_dir / "crossref").exists() or not list((real_project.raw_dir / "crossref").iterdir())


def test_parse_work_keeps_values_as_supplied():
    work = crossref.parse_work({"DOI": "10.5555/ABC.1", "type": "posted-content", "subtype": "preprint",
                                "title": ["  A generated title "], "author": [{"family": "Doe", "given": "A"},
                                                                              {"name": "Synthetic Consortium"}],
                                "published": {"date-parts": [[2020, 5]]}, "relation": {"is-preprint-of": [
                                    {"id-type": "doi", "id": "10.5555/abc.2"}]},
                                "updated-by": [{"type": "Retraction", "DOI": "10.5555/abc.9", "label": "Retraction",
                                                "updated": {"date-time": "2022-01-02T00:00:00Z"}}]})
    assert work["doi"] == "10.5555/abc.1" and work["title"] == "A generated title" and work["year"] == 2020
    assert work["authors"] == ["Doe, A", "Synthetic Consortium"] and work["publication_status"] == "preprint"
    assert work["relations"] == {"is-preprint-of": ["10.5555/abc.2"]}
    assert work["notices"] == [{"type": "retraction", "doi": "10.5555/abc.9", "label": "Retraction",
                                "date": "2022-01-02"}]
    empty = crossref.parse_work({})
    assert empty["title"] is None and empty["year"] is None and empty["publication_status"] == "unknown"


# ------------------------------------------------------------------ short author-year citations
_WORK = {"title": "Tracer release from a stirred source tank", "authors": ["Arden, Q", "Belmont, T"], "year": 2021}


def test_short_citation_agrees_by_first_author_and_year():
    assert crossref.author_year_agreement("[Source tank · dye] Arden 2021 · Fig. 2", _WORK)
    assert crossref.author_year_agreement("Arden 2022 · Fig. 2", _WORK)          # online/print year differ by one
    assert not crossref.author_year_agreement("Arden 2019 · Fig. 2", _WORK)
    assert not crossref.author_year_agreement("Belmont 2021 · Fig. 2", _WORK)    # not the first author
    assert not crossref.author_year_agreement("Arden · Fig. 2", _WORK)          # no year to compare
    assert not crossref.author_year_agreement("Arden 2021", {**_WORK, "authors": []})


def test_identity_agreement_uses_author_and_year_only_for_untitled_short_citations():
    short = {"title": None, "citation_text": "[Source tank · dye] Arden 2021 · Fig. 2\nhttps://doi.org/10.5555/x"}
    assert crossref.identity_agreement(short, _WORK, 0.35) == (None, "first_author_and_year")
    wrong_author = {**short, "citation_text": "Corwin 2021 · Fig. 2"}
    sim, basis = crossref.identity_agreement(wrong_author, _WORK, 0.35)
    assert sim == 0.0 and basis == "title_words_in_citation"                     # still a possible misassignment
    titled = {"title": "An unrelated title about pumps", "citation_text": "Arden 2021"}
    sim, basis = crossref.identity_agreement(titled, _WORK, 0.35)
    assert basis == "title_words" and sim < 0.35                                 # a stored title is always compared
