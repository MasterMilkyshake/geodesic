"""Deduplication: identifier normalisation, duplicate detection, review candidates, reversible groupings."""

from __future__ import annotations

import pytest

from conftest import OPERATOR, REVIEWER_A, REVIEWER_B, cursor_transport, paper_id
from geodesic import audit, dedup, papers
from geodesic.errors import ReviewError, ValidationError
from geodesic.identifiers import normalize_doi, normalize_identifier, normalize_pmcid, normalize_pmid
from geodesic.search import europepmc, fileimport
from geodesic.synthetic import search as SS
from geodesic.util import Actor

T_CONDUIT = "Dye pulse dispersion in a baffled conduit"


def _rels(project, **kw):
    return dedup.relations(project.conn, status=kw.pop("status", None), limit=200, **kw)["rows"]


def _rel(project, a, b, relation_type=None):
    found = dedup.existing_relation(project.conn, a, b, relation_type)
    assert len(found) == 1, found
    return found[0]


# ------------------------------------------------------------------ identifiers
@pytest.mark.parametrize("raw, expected", [
    ("10.5555/GEODESIC.SYNTH.101", "10.5555/geodesic.synth.101"),
    ("https://doi.org/10.5555/geodesic.synth.101", "10.5555/geodesic.synth.101"),
    ("http://dx.doi.org/10.5555/Geodesic.Synth.101.", "10.5555/geodesic.synth.101"),
    ("doi:10.5555/geodesic.synth.101", "10.5555/geodesic.synth.101"),
    ("  DOI: 10.5555/geodesic.synth.101 ", "10.5555/geodesic.synth.101"),
    ("10.5555/geodesic.synth(101)", "10.5555/geodesic.synth(101)"),
    ("10.5555/not a doi", None),
    ("geodesic.synth.101", None),
    ("", None),
    (None, None),
])
def test_doi_normalisation(raw, expected):
    assert normalize_doi(raw) == expected


def test_other_identifier_normalisation():
    assert normalize_pmid(" 90000002 ") == "90000002"
    assert normalize_pmid("PMID: 90000002") == "90000002"
    assert normalize_pmid("not-a-number") is None
    assert normalize_pmcid("pmc9000002") == "PMC9000002"
    assert normalize_pmcid("9000002") in ("PMC9000002", None)
    assert normalize_identifier("doi", "HTTPS://DOI.ORG/10.5555/ABC") == "10.5555/abc"


def test_malformed_identifier_is_kept_as_supplied_and_never_matched(project):
    conn = project.conn
    a = papers.create_paper(conn, origin="manual", resolution_status="resolved", title="Alpha study of a tank")
    b = papers.create_paper(conn, origin="manual", resolution_status="resolved", title="Unrelated pond survey")
    for pid in (a, b):
        out = papers.add_identifier(conn, pid, "doi", "10.5555/not a doi", "manual")
        assert out["validation_status"] == "invalid_syntax"
    raw = [r["value_raw"] for r in conn.execute("SELECT value_raw FROM paper_identifier WHERE scheme = 'doi'")]
    assert raw == ["10.5555/not a doi"] * 2
    stats = dedup.find_duplicates(conn, project.config.dedup)
    assert stats["identifier_matches"] == 0 and _rels(project) == []


# ------------------------------------------------------------------ detection on the search fixture
def test_search_fixture_detection_counts(searched):
    stats = searched.search_out["dedup"]
    assert stats["identifier_matches"] == 1
    assert stats["auto_grouped"] == 0
    assert stats["identifier_conflicts"] == 1
    assert stats["candidates_identifier"] == 0
    assert stats["candidates_similarity"] == 3
    assert dedup.groupings(searched.conn) == []
    # nothing is decided without a reviewer
    assert {r["status"] for r in _rels(searched)} == {"candidate"}


def test_shared_identifier_with_differing_other_identifiers_is_flagged_not_grouped(searched):
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s8 = paper_id(searched, "Thermal stratification of a storage pond")
    rel = _rel(searched, s2, s8)
    assert rel["relation_type"] == "identifier_conflict"
    assert rel["method"] == "shared_identifier_other_identifiers_differ"
    assert rel["status"] == "candidate"
    details = _rels(searched, relation_type="identifier_conflict")[0]["score_details"]
    assert details["scheme"] == "doi" and details["value"] == "10.5555/geodesic.synth.101"
    assert details["other_identifiers_differ"] == ["pmid"]
    assert details["title_similarity"] < searched.config.dedup.identifier_title_conflict_below
    # both source records are kept, each with its own metadata
    assert papers.canonical_id(searched.conn, s8) == s8 and papers.canonical_id(searched.conn, s2) == s2
    events = audit.history(searched.conn, entity_type="paper", entity_id=s8)
    assert "paper.identifier_conflict" in [e["action"] for e in events]
    # a reviewer confirming the conflict marks both identities, and still groups nothing
    out = dedup.decide_relation(searched.conn, rel["id"], REVIEWER_A, "confirmed", note="two different works")
    assert out["grouping_id"] is None
    statuses = {r["identity_status"] for r in searched.conn.execute(
        "SELECT identity_status FROM paper WHERE id IN (?, ?)", (s2, s8))}
    assert statuses == {"conflict"}
    assert dedup.groupings(searched.conn) == []


def test_similar_title_author_year_is_a_review_candidate_with_documented_score(searched):
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    rel = _rel(searched, s2, s4)
    assert (rel["relation_type"], rel["status"], rel["method"]) == \
        ("duplicate", "candidate", "title_author_year_similarity")
    assert rel["score"] == 1.0 and rel["decided_by"] is None
    item = next(r for r in _rels(searched) if r["id"] == rel["id"])
    cfg = searched.config.dedup
    assert item["score_details"]["components"] == {"title": 1.0, "first_author": 1.0, "year": 1.0}
    assert item["score_details"]["threshold"] == cfg.candidate_threshold
    assert item["score_details"]["title_floor"] == cfg.title_floor
    assert item["score_details"]["weights_used"] == {"title": cfg.weight_title, "first_author":
                                                     cfg.weight_first_author, "year": cfg.weight_year}
    assert item["already_grouped"] is False
    # an identical score does not group the records: only a reviewer does
    assert papers.canonical_id(searched.conn, s4) == s4


def test_preprint_and_published_version_are_linked_not_grouped(searched):
    conn = searched.conn
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s3 = paper_id(searched, T_CONDUIT, year=2021)
    rel = _rel(searched, s2, s3)
    assert rel["relation_type"] == "preprint_published" and rel["status"] == "candidate"
    assert rel["score"] == 0.925          # same title and first author, years one apart
    out = dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed", note="preprint of the article")
    assert out["grouping_id"] is None
    assert papers.canonical_id(conn, s3) == s3 and papers.canonical_id(conn, s2) == s2
    assert dedup.groupings(conn) == []
    assert _rel(searched, s2, s3)["status"] == "confirmed"
    assert _rel(searched, s2, s3)["decided_by"] == REVIEWER_A.name


def test_detection_is_idempotent_and_keeps_reviewer_decisions(searched):
    conn = searched.conn
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    rel = _rel(searched, s2, s4)
    dedup.decide_relation(conn, rel["id"], REVIEWER_A, "rejected", note="different reports")
    before = conn.execute("SELECT COUNT(*) FROM paper_relation").fetchone()[0]
    again = dedup.find_duplicates(conn, searched.config.dedup)
    assert again["auto_grouped"] == again["candidates_similarity"] == again["candidates_identifier"] == 0
    assert again["identifier_conflicts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM paper_relation").fetchone()[0] == before
    assert _rel(searched, s2, s4)["status"] == "rejected"


# ------------------------------------------------------------------ scoring
def test_score_components_and_renormalisation(project):
    cfg = project.config.dedup
    base = {"title": "Tracer washout from a stirred tank", "first_author": "rowntree", "year": 2012}
    same = dedup.score_pair(base, dict(base), cfg)
    assert same["score"] == 1.0
    missing = dedup.score_pair(base, {**base, "first_author": None, "year": None}, cfg)
    assert missing == {"score": 1.0, "components": {"title": 1.0, "first_author": None, "year": None},
                       "weights_used": {"title": cfg.weight_title}}
    other_author = dedup.score_pair(base, {**base, "first_author": "sable"}, cfg)
    assert other_author["score"] == pytest.approx(cfg.weight_title + cfg.weight_year)
    far_year = dedup.score_pair(base, {**base, "year": 2020}, cfg)
    assert far_year["score"] == pytest.approx(cfg.weight_title + cfg.weight_first_author)
    no_title = dedup.score_pair(base, {**base, "title": None}, cfg)
    assert no_title["score"] is None
    assert dedup.title_similarity("Dye-pulse dispersion in a baffled conduit.", T_CONDUIT) == 1.0
    assert dedup.title_similarity("Thermal stratification of a storage pond", T_CONDUIT) < 0.35
    assert dedup.title_similarity(None, T_CONDUIT) is None


def test_threshold_is_configurable(project):
    conn = project.conn
    a = papers.create_paper(conn, origin="manual", resolution_status="resolved", year=2020,
                            title="Tracer washout from a stirred tank", authors=["Rowntree E"])
    b = papers.create_paper(conn, origin="manual", resolution_status="resolved", year=2020,
                            title="Tracer washout from a stirred vessel", authors=["Rowntree E"])
    cfg = project.config.dedup
    sim = dedup.title_similarity("Tracer washout from a stirred tank", "Tracer washout from a stirred vessel")
    assert cfg.title_floor < sim < 1.0
    strict = cfg.model_copy(update={"candidate_threshold": 0.99})
    assert dedup.find_duplicates(conn, strict)["candidates_similarity"] == 0
    assert dedup.find_duplicates(conn, cfg)["candidates_similarity"] == 1
    rel = _rel(project, a, b)
    assert rel["status"] == "candidate" and cfg.candidate_threshold <= rel["score"] < 1.0


# ------------------------------------------------------------------ validated vs unvalidated identifiers
def _with_search(project):
    return europepmc.run_search(project, OPERATOR, query="synthetic", page_size=4, mode="fixture",
                                transport=cursor_transport(SS.europepmc_pages(4)))


def test_unvalidated_identifier_match_is_a_candidate_until_validated(imported):
    from geodesic.search import crossref

    project, _ = imported
    conn = project.conn
    out = _with_search(project)
    assert out["dedup"]["auto_grouped"] == 0 and out["dedup"]["candidates_identifier"] >= 1
    s1 = paper_id(project, "Tracer release from a stirred source tank", origin="search")
    wb = conn.execute("SELECT id FROM paper WHERE origin = 'workbook' AND doi = '10.5555/geodesic.synth.001'"
                      ).fetchone()["id"]
    rel = _rel(project, wb, s1, "duplicate")
    assert (rel["status"], rel["method"], rel["score"]) == ("candidate", "identifier_match_unvalidated", None)
    assert papers.canonical_id(conn, s1) == s1
    # the workbook's DOI is a claim of the workbook; the provider's DOI was supplied with the record
    val = {r["paper_id"]: r["validation_status"] for r in conn.execute(
        "SELECT paper_id, validation_status FROM paper_identifier WHERE scheme = 'doi' AND value_norm = ?",
        ("10.5555/geodesic.synth.001",))}
    assert val == {wb: "unvalidated", s1: "validated"}

    stats = crossref.validate_identifiers(project, OPERATOR, transport=SS.crossref_transport())
    assert stats["dedup"]["auto_grouped"] == 1
    rel = _rel(project, wb, s1, "duplicate")
    assert rel["status"] == "confirmed" and rel["decided_by"] == dedup.AUTOMATED.name
    assert rel["decision_note"] == "identifier since validated on both records"
    # the earlier record is canonical; the later source record is kept, not deleted
    assert papers.canonical_id(conn, s1) == wb
    grouping = dedup.groupings(conn)
    assert len(grouping) == 1
    assert (grouping[0]["canonical_paper_id"], grouping[0]["member_paper_id"], grouping[0]["basis"],
            grouping[0]["created_by"]) == (wb, s1, "validated_identifier_match", dedup.AUTOMATED.name)
    assert conn.execute("SELECT record_status FROM paper WHERE id = ?", (s1,)).fetchone()[0] == "active"
    # its membership of the search run is preserved
    run_id = out["run"]["id"]
    assert conn.execute("SELECT COUNT(*) FROM search_result WHERE search_run_id = ? AND paper_id = ?",
                        (run_id, s1)).fetchone()[0] == 1
    actions = [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=s1)]
    assert "paper.group" in actions


def test_grouping_reversal_is_recorded_and_sticky(imported):
    from geodesic.search import crossref

    project, _ = imported
    conn = project.conn
    _with_search(project)
    crossref.validate_identifiers(project, OPERATOR, transport=SS.crossref_transport())
    s1 = paper_id(project, "Tracer release from a stirred source tank", origin="search")
    wb = papers.canonical_id(conn, s1)
    assert wb != s1
    with pytest.raises(ReviewError, match="reason"):
        dedup.ungroup(conn, s1, REVIEWER_A, "  ")
    with pytest.raises(ReviewError, match="named reviewer"):
        dedup.ungroup(conn, s1, dedup.AUTOMATED, "automatic")
    gid = dedup.ungroup(conn, s1, REVIEWER_A, "different editions")
    assert papers.canonical_id(conn, s1) == s1
    # the grouping row stays as history
    row = dict(conn.execute("SELECT * FROM paper_merge WHERE id = ?", (gid,)).fetchone())
    assert (row["status"], row["reversed_by"], row["reversal_reason"]) == \
        ("reversed", REVIEWER_A.name, "different editions")
    assert row["reversed_at"] and row["created_by"] == dedup.AUTOMATED.name
    assert dedup.groupings(conn) == [] and len(dedup.groupings(conn, status=None)) == 1
    rel = _rel(project, wb, s1, "duplicate")
    assert rel["status"] == "rejected" and "grouping reversed" in rel["decision_note"]
    assert [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=s1)].count("paper.ungroup") == 1
    # detection never groups the pair again
    again = dedup.find_duplicates(conn, project.config.dedup)
    assert again["auto_grouped"] == 0 and papers.canonical_id(conn, s1) == s1
    with pytest.raises(ReviewError, match="not currently grouped"):
        dedup.ungroup(conn, s1, REVIEWER_A, "again")
    # a reviewer can still group them again explicitly; history then shows both groupings
    out = dedup.decide_relation(conn, rel["id"], REVIEWER_B, "confirmed", note="same article after all")
    assert out["grouping_id"] and papers.canonical_id(conn, s1) == wb
    assert [g["status"] for g in dedup.groupings(conn, status=None)] == ["reversed", "active"]


# ------------------------------------------------------------------ reviewer decisions
def test_reviewer_confirms_a_duplicate_and_reverses_it(searched):
    conn = searched.conn
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    rel = _rel(searched, s2, s4)
    with pytest.raises(ReviewError):
        dedup.decide_relation(conn, rel["id"], dedup.AUTOMATED, "confirmed")
    with pytest.raises(ValidationError):
        dedup.decide_relation(conn, rel["id"], REVIEWER_A, "maybe")
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("paper", "search_result")}
    out = dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed", note="same article")
    assert out["grouping_id"]
    assert papers.canonical_id(conn, s4) == s2
    g = dedup.groupings(conn)[0]
    assert (g["basis"], g["created_by"], g["relation_id"]) == ("reviewer_confirmed_duplicate", REVIEWER_A.name,
                                                               rel["id"])
    # grouping removes nothing: both records and both search-result rows remain, metadata as supplied
    assert counts == {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts}
    assert conn.execute("SELECT title FROM paper WHERE id = ?", (s4,)).fetchone()[0] == \
        "Dye-pulse dispersion in a baffled conduit."
    assert conn.execute("SELECT COUNT(*) FROM search_result WHERE paper_id = ?", (s4,)).fetchone()[0] == 1
    with pytest.raises(ValidationError, match="already grouped"):
        dedup.group(conn, s2, s4, REVIEWER_A, "manual")
    dedup.ungroup(conn, s4, REVIEWER_B, "confirmed by mistake")
    assert papers.canonical_id(conn, s4) == s4
    assert _rel(searched, s2, s4)["status"] == "rejected"
    history = [e["action"] for e in audit.history(conn, entity_type="paper", entity_id=s4)]
    assert history.count("paper.group") == 1 and history.count("paper.ungroup") == 1
    assert audit.verify_chain(conn)[0] is True


def test_reviewer_can_choose_the_canonical_record(searched):
    conn = searched.conn
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    rel = _rel(searched, s2, s4)
    with pytest.raises(ValidationError, match="canonical"):
        dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed",
                              canonical=paper_id(searched, "Sediment sampling protocol"))
    assert _rel(searched, s2, s4)["status"] == "candidate"      # the failed decision left nothing behind
    dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed", canonical=s4)
    assert papers.canonical_id(conn, s2) == s4


def test_duplicate_candidate_reclassified_as_reports_of_the_same_study(searched):
    conn = searched.conn
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    rel = _rel(searched, s2, s4)
    out = dedup.decide_relation(conn, rel["id"], REVIEWER_A, "confirmed", as_type="same_study_report",
                                note="conference report and article of one study")
    assert out["reclassified_from"] == rel["id"] and out["grouping_id"] is None
    assert _rel(searched, s2, s4, "duplicate")["status"] == "rejected"
    new = _rel(searched, s2, s4, "same_study_report")
    assert new["status"] == "confirmed" and new["method"] == "reviewer" and new["id"] == out["relation_id"]
    # reports of one study stay separate records
    assert papers.canonical_id(conn, s4) == s4 and dedup.groupings(conn) == []
    with pytest.raises(ValidationError):
        dedup.decide_relation(conn, rel["id"], REVIEWER_A, "rejected", as_type="preprint_published")


def test_reviewer_links_two_records_directly(searched):
    conn = searched.conn
    s6 = paper_id(searched, "Tracer washout from a stirred tank")
    s7 = paper_id(searched, "Reactive tracer decay in a weir pool")
    out = dedup.relate(conn, s6, s7, "same_study_report", REVIEWER_A, note="two reports of one campaign")
    assert out["status"] == "confirmed" and out["grouping_id"] is None
    rel = _rel(searched, s6, s7)
    assert (rel["relation_type"], rel["status"], rel["decided_by"]) == ("same_study_report", "confirmed",
                                                                      REVIEWER_A.name)
    with pytest.raises(ReviewError):
        dedup.relate(conn, s6, s7, "duplicate", dedup.AUTOMATED)
    with pytest.raises(ValidationError):
        dedup.relate(conn, s6, s6, "duplicate", REVIEWER_A)
    with pytest.raises(ValidationError):
        dedup.relate(conn, s6, s7, "sequel", REVIEWER_A)
    grouped = dedup.relate(conn, s6, s7, "duplicate", REVIEWER_A)
    assert grouped["grouping_id"] and papers.canonical_id(conn, s7) == s6


def test_simulated_reviewer_only_in_synthetic_projects(searched, real_project):
    sim = Actor("Simulated reviewer", "simulated")
    s2 = paper_id(searched, T_CONDUIT, year=2022)
    s4 = paper_id(searched, "Dye-pulse dispersion in a baffled conduit.")
    assert dedup.decide_relation(searched.conn, _rel(searched, s2, s4)["id"], sim, "confirmed")["grouping_id"]
    conn = real_project.conn
    a = papers.create_paper(conn, origin="manual", resolution_status="resolved", title="Generated test record one")
    b = papers.create_paper(conn, origin="manual", resolution_status="resolved", title="Generated test record two")
    with pytest.raises(ReviewError, match="synthetic"):
        dedup.relate(conn, a, b, "duplicate", sim)


# ------------------------------------------------------------------ file imports and bulk confirmation
def test_file_record_sharing_an_identifier_is_a_candidate_and_bulk_confirmation(searched, tmp_path):
    conn = searched.conn
    out = fileimport.import_search_file(searched, SS.write_ris_fixture(tmp_path / "in" / "refs.ris"), OPERATOR)
    assert out["dedup"]["candidates_identifier"] == 1 and out["dedup"]["auto_grouped"] == 0
    s2 = paper_id(searched, T_CONDUIT, origin="search", year=2022)
    ris = paper_id(searched, T_CONDUIT, origin="file_import")
    rel = _rel(searched, s2, ris, "duplicate")
    assert rel["method"] == "identifier_match_unvalidated" and rel["status"] == "candidate"
    with pytest.raises(ReviewError):
        dedup.confirm_identifier_candidates(conn, dedup.AUTOMATED)
    stats = dedup.confirm_identifier_candidates(conn, REVIEWER_A, min_title_similarity=0.9)
    assert stats == {"confirmed": 1, "left_for_review": 0}
    assert papers.canonical_id(conn, ris) == s2
    rel = _rel(searched, s2, ris, "duplicate")
    assert rel["decided_by"] == REVIEWER_A.name and "bulk confirmation" in rel["decision_note"]
    # the file record keeps its membership of the file-import run; the provider record that of the search
    runs = {r["provider"] for r in conn.execute(
        "SELECT sr.provider FROM search_result x JOIN search_run sr ON sr.id = x.search_run_id"
        " WHERE x.paper_id IN (?, ?)", (s2, ris))}
    assert runs == {"europepmc", "file:ris"}
    # similarity candidates are not touched by the bulk confirmation
    assert any(r["method"] == "title_author_year_similarity" and r["status"] == "candidate" for r in _rels(searched))
