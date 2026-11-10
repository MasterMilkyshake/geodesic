"""Reviewable evidence extraction from documents: candidates, manual extraction, grounding, hierarchy."""

from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import ALL_CONFIRMED, OPERATOR, REVIEWER_A, REVIEWER_B, audit_details
from geodesic import audit, findings as F, locations, review
from geodesic.documents import store
from geodesic.errors import GroundingError, ReviewError, ValidationError
from geodesic.extraction import candidates, hierarchy, manual
from geodesic.util import Actor

TANK = "The source tank held 250 L of water and was stirred at 120 rpm."
TABLE = "body[1]/sec[2]/table-wrap[1]"


# ------------------------------------------------------------------ deterministic candidate detection
def test_candidate_detection_is_deterministic_and_anchored(evidence):
    conn = evidence.conn
    pdf = candidates.list_candidates(conn, snapshot_id=evidence.pdf_snap)
    assert [(c["unit_key"], c["char_start"], c["char_end"], c["literal"]) for c in pdf] == [
        ("0", 226, 227, "1"), ("0", 257, 262, "250 L"), ("0", 291, 294, "120"), ("0", 314, 317, "5 g"),
        ("1", 0, 1, "2"), ("1", 33, 41, "62 ± 5 %"), ("1", 105, 129, "approximately 0.04 1/min"),
        ("1", 131, 150, "Between 85 and 95 %"), ("1", 199, 212, "Less than 2 %"), ("1", 297, 306, "18.5 mg/L"),
        ("2", 0, 1, "3")]
    assert all(c["location_kind"] == "pdf_page" and c["status"] == "open" and c["finding_id"] is None for c in pdf)
    # every candidate literal is exactly the stored text at its address
    for c in candidates.list_candidates(conn, status=None, limit=None):
        resolves, text, problem = locations.resolve(conn, candidates.candidate_location(c))
        assert resolves and text == c["literal"], (c["literal"], problem)
        assert c["literal"] in c["context_excerpt"]
        assert c["detector"] == "geodesic-numeric-candidates" and c["detector_version"] == "1.0"

    xml = candidates.list_candidates(conn, snapshot_id=evidence.xml_snap)
    assert sum(c["location_kind"] == "xml_element" for c in xml) == 8
    assert sum(c["location_kind"] == "table_cell" for c in xml) == 8
    cell = evidence.candidate(evidence.xml_snap, "23 ± 2")
    assert (cell["table_key"], cell["row_idx"], cell["col_idx"]) == (TABLE, 2, 1)
    assert cell["parsed"]["column_header"] == "Transit time (min)" and cell["parsed"]["row_header"] == "10"
    # words are not turned into numbers: the 'not reported' cell yields no candidate
    assert not [c for c in xml if c["row_idx"] == 3 and c["col_idx"] == 2]


def test_detection_twice_adds_nothing_and_creates_no_findings(evidence):
    conn = evidence.conn
    n_findings = conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0]
    before = [(c["id"], c["literal"]) for c in candidates.list_candidates(conn, status=None, limit=None)]
    again = candidates.detect(evidence.project, evidence.pdf_snap, OPERATOR)
    assert again["already_detected"] is True and again["new"] == 0 and again["candidates"] == 11
    assert [(c["id"], c["literal"]) for c in candidates.list_candidates(conn, status=None, limit=None)] == before
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == n_findings == 32
    assert candidates.counts(conn, evidence.pdf_snap) == {"open": 11, "promoted": 0, "dismissed": 0}
    with_unit = candidates.list_candidates(conn, snapshot_id=evidence.pdf_snap, with_unit_only=True)
    assert len(with_unit) == 7 and all(c["parsed"]["has_unit"] for c in with_unit)


def test_candidates_are_flagged_not_interpreted(evidence):
    by = {c["literal"]: c["parsed"]["quantity"] for c in
          candidates.list_candidates(evidence.conn, status=None, limit=None) if c["location_kind"] != "table_cell"}
    assert by["62 ± 5 %"]["flags"] == ["percent_reference_needs_review", "uncertainty_type_unspecified"]
    assert by["12 m"]["flags"] == ["unit_ambiguous"]
    assert "from_to_may_be_change_not_range" in by["ranged from 0.8 to 1.4 m"]["flags"]
    assert by["120"]["flags"] == ["no_unit_detected"]
    assert by["Less than 2 %"]["statement_type"] == "bound" and by["Between 85 and 95 %"]["statement_type"] == "range"
    assert by["approximately 0.04 1/min"]["statement_type"] == "approximate"


# ------------------------------------------------------------------ promotion
def test_promoted_candidate_becomes_an_unreviewed_grounded_finding(evidence):
    conn, project = evidence.conn, evidence.project
    cand = evidence.candidate(evidence.pdf_snap, "62 ± 5 %")
    f = candidates.promote(project, cand["id"], REVIEWER_A, changes={"outcome": "tracer released"},
                           stage_keys=["release"])
    c = f["content"]
    assert (f["origin"], f["review_status"], f["grounding_status"], f["source_scope"]) == \
        ("candidate_promotion", "unreviewed", "grounded", "full_text")
    assert c["source_wording"] == "Tracer release reached 62 ± 5 % of the loaded dose after 30 min."
    assert (c["value_literal"], c["value_num"], c["unit_original"]) == ("62", 62.0, "%")
    assert (c["uncertainty_literal"], c["uncertainty_type"]) == ("5", "unspecified_plus_minus")   # '±' is not read as SD
    assert (c["timepoint_literal"], c["relative_semantics"], c["reference_definition"]) == \
        ("30 min", "percent_of_reference", "loaded dose")
    assert c["field_status"]["sample_size"] == "not_yet_extracted" and "outcome" not in c["field_status"]
    assert f["review_flags"] == ["percent_reference_needs_review", "uncertainty_type_unspecified"]
    assert [s["key"] for s in f["stages"]] == ["release"]
    assert f["paper_id"] == evidence.pdf_paper

    # each literal is the stored page text at its own span
    page = store.units(conn, evidence.pdf_snap)[1]["text"]
    for field, (start, end) in f["spans"].items():
        assert page[start:end] == c[field]
    (loc,) = f["locations"]
    assert (loc["kind"], loc["unit_key"], loc["char_start"], loc["char_end"], loc["resolves"]) == \
        ("pdf_page", "1", 10, 74, 1)
    assert page[10:74] == c["source_wording"]

    after = candidates.get_candidate(conn, cand["id"])
    assert (after["status"], after["finding_id"], after["resolved_by"]) == ("promoted", f["id"], REVIEWER_A.name)
    with pytest.raises(ReviewError, match="already 'promoted'"):
        candidates.promote(project, cand["id"], REVIEWER_A)
    assert F.versions(conn, f["id"])[0]["change_kind"] == "candidate_promotion"
    assert audit_details(conn, "candidate.promote")[0]["finding_id"] == f["id"]


def test_table_cell_candidate_takes_its_unit_from_the_column_header_with_its_own_location(evidence):
    cand = evidence.candidate(evidence.xml_snap, "23 ± 2")
    f = candidates.promote(evidence.project, cand["id"], REVIEWER_B, changes={"outcome": "transit time"},
                           stage_keys=["transit"])
    assert f["content"]["unit_original"] == "min" and "unit_from_column_header" in f["review_flags"]
    assert f["spans"]["unit_original"] == {"role": "context:unit_original", "start": 14, "end": 17}
    roles = {l["role"]: l for l in f["locations"]}
    assert set(roles) == {"supporting_excerpt", "context:unit_original"}
    header = roles["context:unit_original"]
    assert (header["table_key"], header["row_idx"], header["col_idx"], header["excerpt"]) == \
        (TABLE, 0, 1, "Transit time (min)")
    assert header["excerpt"][14:17] == "min" and header["resolves"] == 1
    assert (roles["supporting_excerpt"]["row_idx"], roles["supporting_excerpt"]["excerpt"]) == (2, "23 ± 2")
    assert f["grounding_status"] == "grounded"


def test_dismissed_candidates_stay_in_the_record(evidence):
    conn = evidence.conn
    bare = [c["id"] for c in candidates.list_candidates(conn, snapshot_id=evidence.pdf_snap)
            if not c["parsed"]["has_unit"]]
    assert candidates.dismiss(conn, bare, REVIEWER_A, "section numbers and a stirring speed") == 4
    assert candidates.counts(conn, evidence.pdf_snap) == {"open": 7, "promoted": 0, "dismissed": 4}
    assert conn.execute("SELECT COUNT(*) FROM numeric_candidate WHERE snapshot_id = ?",
                        (evidence.pdf_snap,)).fetchone()[0] == 11
    with pytest.raises(ReviewError):
        candidates.dismiss(conn, bare[:1], REVIEWER_A)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM numeric_candidate")
    assert audit_details(conn, "candidate.dismiss")[0]["count"] == 4


def test_candidates_need_a_named_reviewer_to_become_findings(evidence):
    cand = evidence.candidate(evidence.pdf_snap, "18.5 mg/L")
    with pytest.raises(ReviewError, match="named reviewer"):
        candidates.promote(evidence.project, cand["id"], Actor("geodesic", "system"))
    assert candidates.get_candidate(evidence.conn, cand["id"])["status"] == "open"


# ------------------------------------------------------------------ manual extraction
def test_manual_extraction_from_a_quoted_sentence(evidence):
    conn, project = evidence.conn, evidence.project
    loc = manual.build_location(conn, evidence.pdf_snap, unit_key="1",
                                quote="The release rate constant was approximately 0.04 1/min.")
    assert (loc["kind"], loc["char_start"], loc["char_end"]) == ("pdf_page", 75, 130)
    (prop,) = manual.propose_from_span(project, loc)
    assert prop["spans"] == {"value_literal": [119, 123], "unit_original": [124, 129]}
    n_before = conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0]          # proposing stores nothing
    f = manual.create_finding(project, evidence.pdf_paper, REVIEWER_A, location=loc, spans=prop["spans"],
                              content={**prop["content"], "outcome": "release rate constant"},
                              stage_keys=["release"], note="synthetic manual extraction")
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == n_before + 1
    c = f["content"]
    assert (f["origin"], f["review_status"], f["grounding_status"]) == ("manual_extraction", "unreviewed", "grounded")
    assert (c["statement_type"], c["value_literal"], c["value_num"], c["unit_original"]) == \
        ("approximate", "0.04", 0.04, "1/min")
    assert [q["text"] for q in c["qualifiers"]] == ["approximately"]            # the qualifier is kept
    assert c["field_status"]["timepoint"] == "not_yet_extracted"
    version = F.versions(conn, f["id"])[0]
    assert (version["change_kind"], version["created_by"]) == ("manual_create", REVIEWER_A.name)


def test_manual_extraction_locates_literals_inside_the_excerpt(evidence):
    conn, project = evidence.conn, evidence.project
    loc = manual.build_location(conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    f = manual.create_finding(project, evidence.pdf_paper, REVIEWER_A, location=loc, content={
        "kind": "measurement", "statement_type": "exact", "value_literal": "250", "value_num": 250,
        "unit_original": "L", "outcome": "tank volume"})
    assert f["spans"] == {"value_literal": [257, 260], "unit_original": [261, 262]}
    page = store.units(conn, evidence.pdf_snap)[0]["text"]
    assert page[257:260] == "250" and page[261:262] == "L"

    cell = manual.build_location(conn, evidence.xml_snap, table_key=TABLE, row_idx=1, col_idx=1)
    assert (cell["kind"], cell["excerpt"]) == ("table_cell", "42 ± 3")
    g = manual.create_finding(project, evidence.xml_paper, REVIEWER_B, location=cell, content={
        "kind": "measurement", "statement_type": "exact", "value_literal": "42", "value_num": 42,
        "uncertainty_literal": "3", "uncertainty_value": 3, "uncertainty_type": "unspecified_plus_minus",
        "outcome": "transit time"})
    assert g["spans"] == {"value_literal": [0, 2], "uncertainty_literal": [5, 6]}
    assert g["content"]["field_status"]["unit_original"] == "not_yet_extracted"   # the unit was not transcribed

    lim = manual.create_finding(project, evidence.pdf_paper, REVIEWER_A, content={"kind": "limitation"},
                                location=manual.build_location(conn, evidence.pdf_snap, unit_key="2",
                                                               char_start=0, char_end=13))
    assert (lim["kind"], lim["content"]["source_wording"], lim["content"]["statement_type"]) == \
        ("limitation", "3 Limitations", "qualitative")


@pytest.mark.parametrize("content, field, reason", [
    ({"statement_type": "exact", "value_literal": "260", "value_num": 260, "unit_original": "L"},
     "value_literal", "claimed source string is absent from the specified span"),
    ({"statement_type": "exact", "value_literal": "25", "value_num": 25, "unit_original": "L"},
     "value_literal", "the specified span ends inside a longer number in the source text"),
    ({"statement_type": "exact", "value_literal": "250", "value_num": 250, "unit_original": "mL"},
     "unit_original", "claimed source string is absent from the specified span"),
    ({"statement_type": "exact", "value_literal": "250", "value_num": 25, "unit_original": "L"},
     "value_num", "value_num = 25 is not a number written in value_literal '250'"),
    ({"source_wording": "The tank held 300 L."},
     "source_wording", "claimed source string is absent from the specified span"),
])
def test_transcription_absent_from_the_span_is_rejected_and_logged(evidence, content, field, reason):
    conn = evidence.conn
    loc = manual.build_location(conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    n_findings = conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0]
    with pytest.raises(GroundingError, match="refused and logged as a parse failure"):
        manual.create_finding(evidence.project, evidence.pdf_paper, REVIEWER_A, location=loc, content=content)
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == n_findings
    rows = [dict(r) for r in conn.execute(
        "SELECT context, snapshot_id, unit_key, field, claimed_literal, reason FROM parse_failure")]
    assert len(rows) == 1
    assert (rows[0]["context"], rows[0]["snapshot_id"], rows[0]["unit_key"], rows[0]["field"], rows[0]["reason"]) == \
        ("manual_extraction", evidence.pdf_snap, "0", field, reason)
    (event,) = audit_details(conn, "finding.extraction_refused")
    assert event["problems"][0]["field"] == field


def test_explicit_span_must_hold_exactly_the_claimed_literal(evidence):
    conn = evidence.conn
    loc = manual.build_location(conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    with pytest.raises(GroundingError):
        manual.create_finding(evidence.project, evidence.pdf_paper, REVIEWER_A, location=loc,
                              content={"statement_type": "exact", "value_literal": "250", "value_num": 250},
                              spans={"value_literal": [291, 294]})         # that span holds '120'
    assert F.span_cuts_token("held 250 L", 5, 7) == "the specified span ends inside a longer number in the source text"
    assert F.span_cuts_token("held 250 L", 6, 8) == "the specified span starts inside a longer number in the source text"
    assert F.span_cuts_token("18.5 mg/L", 0, 2) == "the specified span ends inside a longer number in the source text"
    assert F.span_cuts_token("18.5 mg/L", 6, 9) == "the specified span starts inside a longer word in the source text"
    assert F.span_cuts_token("held 250 L", 5, 8) is None and F.span_cuts_token("62 ± 5 %", 0, 2) is None
    assert F.span_cuts_token("ends at 250.", 8, 11) is None            # a sentence full stop is not a decimal point


def test_location_and_paper_are_checked_before_anything_is_stored(evidence):
    conn, project = evidence.conn, evidence.project
    with pytest.raises(GroundingError, match="does not occur at this location"):
        manual.build_location(conn, evidence.pdf_snap, unit_key="0", quote="not in the document")
    with pytest.raises(GroundingError, match="has no text unit"):
        manual.build_location(conn, evidence.pdf_snap, unit_key="9", char_start=0, char_end=5)
    with pytest.raises(GroundingError, match="does not resolve"):
        manual.build_location(conn, evidence.pdf_snap, unit_key="0", char_start=0, char_end=99999)
    loc = manual.build_location(conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    with pytest.raises(ValidationError, match="belongs to paper"):
        manual.create_finding(project, evidence.xml_paper, REVIEWER_A, location=loc, content={})
    with pytest.raises(ReviewError, match="named reviewer"):
        manual.create_finding(project, evidence.pdf_paper, Actor("geodesic", "system"), location=loc, content={})
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE origin != 'workbook_import'").fetchone()[0] == 0


def test_proposals_for_a_sentence_with_two_quantities_are_separate_and_flagged(evidence):
    loc = manual.build_location(evidence.conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    props = manual.propose_from_span(evidence.project, loc)
    assert [p["content"]["value_literal"] for p in props] == ["250", "120"]
    assert "multiple_quantities_in_segment" in props[0]["flags"] and "no_unit_detected" in props[1]["flags"]
    f = manual.create_finding(evidence.project, evidence.pdf_paper, REVIEWER_A, location=loc,
                              content=props[0]["content"], spans=props[0]["spans"], flags=props[0]["flags"])
    assert f["review_status"] == "draft"                       # ambiguous extraction is stored as a draft


# ------------------------------------------------------------------ review of extracted findings
def test_extracted_finding_is_not_verified_until_a_reviewer_confirms_each_check(evidence):
    conn = evidence.conn
    cand = evidence.candidate(evidence.pdf_snap, "18.5 mg/L")
    f = candidates.promote(evidence.project, cand["id"], REVIEWER_A, changes={"outcome": "outflow concentration"})
    assert f["review_status"] == "unreviewed"
    with pytest.raises(ReviewError, match="Verification needs each of these checked"):
        review.verify(conn, f["id"], REVIEWER_B, {"outcome": "confirmed"})
    review.verify(conn, f["id"], REVIEWER_B, ALL_CONFIRMED)
    got = F.get_finding(conn, f["id"])
    assert got["review_status"] == "verified"
    assert [(r["action"], r["reviewer"]) for r in F.reviews(conn, f["id"])] == [("verify", REVIEWER_B.name)]

    # an edit makes a new version; the earlier verification no longer applies and the old version is kept
    out = review.edit(conn, f["id"], REVIEWER_A, {"outcome": "peak outflow concentration"})
    assert out["version_no"] == 2 and out["changed_fields"] == ["outcome"]
    got = F.get_finding(conn, f["id"])
    assert got["review_status"] == "unreviewed" and got["current_version"] == 2
    vs = F.versions(conn, f["id"])
    assert [v["version_no"] for v in vs] == [1, 2]
    assert vs[0]["snapshot"]["content"]["outcome"] == "outflow concentration"
    assert audit.verify_chain(conn)[0] is True


def test_edit_that_breaks_grounding_makes_the_finding_unsupported_and_unverifiable(evidence):
    conn = evidence.conn
    cand = evidence.candidate(evidence.pdf_snap, "18.5 mg/L")
    f = candidates.promote(evidence.project, cand["id"], REVIEWER_A)
    out = review.edit(conn, f["id"], REVIEWER_A, {"value_literal": "19.5", "value_num": 19.5})
    assert out["grounding_status"] == "unsupported"
    assert out["grounding_problems"][0]["field"] == "value_literal"
    with pytest.raises(ReviewError, match="unsupported"):
        review.verify(conn, f["id"], REVIEWER_B, ALL_CONFIRMED)
    assert F.get_finding(conn, f["id"])["review_status"] != "verified"


# ------------------------------------------------------------------ sparse hierarchy
def test_hierarchy_levels_are_optional_and_checked_for_consistency(evidence):
    conn, project = evidence.conn, evidence.project
    f1 = candidates.promote(project, evidence.candidate(evidence.pdf_snap, "62 ± 5 %")["id"], REVIEWER_A)
    f2 = candidates.promote(project, evidence.candidate(evidence.xml_snap, "42 ± 3 min")["id"], REVIEWER_A)
    assert (f1["study_id"], f1["experiment_id"], f1["comparison_group_id"]) == (None, None, None)

    study = hierarchy.create_study(conn, REVIEWER_A, "Stirred tank study", paper_ids=[evidence.pdf_paper])
    exp = hierarchy.create_experiment(conn, REVIEWER_A, evidence.pdf_paper, "Run series 1", study_id=study["id"])
    grp = hierarchy.create_group(conn, REVIEWER_A, exp["id"], "120 rpm", role="condition")
    out = hierarchy.assign(conn, f1["id"], REVIEWER_A, comparison_group_id=grp["id"])
    # the group implies its experiment and study
    assert (out["study_id"], out["experiment_id"], out["comparison_group_id"]) == (study["id"], exp["id"], grp["id"])
    assert out["version_no"] == 2
    with pytest.raises(ValidationError, match="not in the finding's paper"):
        hierarchy.assign(conn, f2["id"], REVIEWER_A, experiment_id=exp["id"])
    assert F.get_finding(conn, f2["id"])["experiment_id"] is None

    tree = hierarchy.tree(conn, evidence.pdf_paper)
    assert tree["studies"][0]["papers"] == [evidence.pdf_paper] and tree["studies"][0]["n_findings"] == 1
    assert tree["experiments"][0]["groups"][0]["label"] == "120 rpm"

    # a level in use is not removed silently; removal keeps the record
    with pytest.raises(ValidationError):
        hierarchy.remove(conn, "group", grp["id"], REVIEWER_A, "no longer needed")
    hierarchy.assign(conn, f1["id"], REVIEWER_A, clear=True)
    hierarchy.remove(conn, "group", grp["id"], REVIEWER_A, "no longer needed")
    assert conn.execute("SELECT record_status FROM comparison_group WHERE id = ?", (grp["id"],)).fetchone()[0] == "removed"
