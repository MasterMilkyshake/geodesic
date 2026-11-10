"""Compatibility assessment before comparison, independence counting and reviewer vocabulary.

Everything is synthetic. Nothing here pools or averages values.
"""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ALL_CONFIRMED, OPERATOR, REVIEWER_A, REVIEWER_B
from geodesic import audit, comparison, review, vocabulary
from geodesic.errors import NotFoundError, ReviewError, ValidationError
from geodesic.extraction import hierarchy
from geodesic.util import Actor


def fid(conn, literal, unit):
    return conn.execute("SELECT id FROM finding WHERE value_literal = ? AND unit_original = ?", (literal, unit)).fetchone()[0]


def check(assessment, key):
    return next(c for c in assessment["checks"] if c["check"] == key)


@pytest.fixture
def pair(imported):
    """Two synthetic concentration findings of different papers, both verified by a reviewer."""
    project, _ = imported
    a, b = fid(project.conn, "12.5", "mg/L"), fid(project.conn, "18", "mg/L")
    for f in (a, b):
        review.verify(project.conn, f, REVIEWER_A, ALL_CONFIRMED, acknowledge_flags=True)
    return project, a, b


def test_unverified_findings_are_never_a_quantitative_candidate(imported):
    project, _ = imported
    conn = project.conn
    a, b = fid(conn, "12.5", "mg/L"), fid(conn, "18", "mg/L")
    out = comparison.assess(conn, [a, b])
    assert out["classification"] == "descriptive_only" and out["blocking"] == ["verification"]
    assert "never pooled" in out["statement"]
    with pytest.raises(ValidationError, match="blocking checks: verification"):
        comparison.save_set(conn, "x", [a, b], REVIEWER_A, classification="quantitative_candidate")
    with pytest.raises(ValidationError, match="not verified against its source"):
        comparison.set_eligibility(conn, [a], "quantitative_candidate", REVIEWER_A)
    saved = comparison.save_set(conn, "descriptive", [a, b], REVIEWER_A)       # descriptive sets are always allowed
    assert saved["classification"] == "descriptive_only"
    assert conn.execute("SELECT COUNT(*) FROM comparison_set").fetchone()[0] == 1


def test_compatible_verified_findings_are_a_candidate_with_cautions(pair):
    project, a, b = pair
    out = comparison.assess(project.conn, [a, b])
    assert out["classification"] == "quantitative_candidate" and out["blocking"] == []
    assert out["cautions"] == ["system_context", "uncertainty", "sample_size", "independence"]
    assert check(out, "unit")["detail"] == "Same unit as written (mg/L)."
    assert check(out, "uncertainty")["values"] == ["none recorded", "sd"]
    assert out["independence"]["independent_units_at_most"] == 2
    assert out["independence"]["findings_independence_not_assessed"] == 2 and out["independence"]["reports"] == 2
    assert project.conn.execute("SELECT COUNT(*) FROM comparison_set").fetchone()[0] == 0      # assess stores nothing


def test_blocking_differences_are_named(pair):
    project, a, b = pair
    conn = project.conn
    bound, per_mass, percent = fid(conn, "0.5", "mg/L"), fid(conn, "0.05", "mg/kg"), fid(conn, "90", "%")
    out = comparison.assess(conn, [a, b, bound])
    assert out["blocking"] == ["verification", "outcome"] and check(out, "outcome")["status"] == "missing"
    assert check(out, "statement_form")["values"] == ["bound", "exact"]
    out = comparison.assess(conn, [a, per_mass])
    assert "unit" in out["blocking"] and "different dimensions" in check(out, "unit")["detail"]
    out = comparison.assess(conn, [a, percent])
    assert "unit" in out["blocking"] and "never converted" in check(out, "unit")["detail"]
    assert comparison.assess(conn, [a])["blocking"] == ["set_size"]
    with pytest.raises(NotFoundError):
        comparison.assess(conn, [a, "fnd_missing"])


def test_relative_quantities_need_identical_unit_meaning_and_reference(imported):
    project, _ = imported
    conn = project.conn
    rows = [r["id"] for r in conn.execute(
        "SELECT id FROM finding WHERE value_literal = '42' AND unit_original = '%' ORDER BY pk")]
    assert len(rows) == 2                                   # same wording in two stages of the fixture workbook
    unit = check(comparison.assess(conn, rows), "unit")
    assert unit["status"] == "ok" and unit["values"]["reference_definitions"] == ["loaded mass"]
    other = fid(conn, "90", "%")                             # percent of another reference
    unit = check(comparison.assess(conn, [rows[0], other]), "unit")
    assert unit["status"] == "differs" and unit["values"]["reference_definitions"] == ["loaded mass", "tracer"]
    unspecified = fid(conn, "15", "%")
    assert check(comparison.assess(conn, [rows[0], unspecified]), "unit")["status"] == "differs"


def test_dependent_findings_are_not_counted_as_independent(pair):
    project, a, b = pair
    conn = project.conn
    third = fid(conn, "0.12", "1/h")
    units = comparison.independence_units(conn)
    assert comparison.count_units(units, [a, b, third]) == {
        "findings": 3, "independent_units_at_most": 3,
        "units_by_basis": {"experiment": 0, "dependency_relation": 0, "not_assessed": 3},
        "findings_independence_not_assessed": 3}

    # a reviewer records that the two findings come from the same samples
    review.relate(conn, a, b, "dependent", REVIEWER_A, note="synthetic: same samples")
    units = comparison.independence_units(conn)
    assert units[a]["unit"] == units[b]["unit"] and units[a]["basis"] == "dependency_relation"
    counts = comparison.count_units(units, [a, b, third])
    assert counts["findings"] == 3 and counts["independent_units_at_most"] == 2
    out = comparison.assess(conn, [a, b])
    assert out["independence"]["independent_units_at_most"] == 1 and out["independence"]["reports"] == 2
    ind = check(out, "independence")
    assert ind["status"] == "differs" and "must not be counted as independent observations" in ind["detail"]
    rows = comparison.table(conn, [a, b])["rows"]
    assert rows[0]["independence_unit"] == rows[1]["independence_unit"]

    # a rejected relation does not make findings dependent; a chain of relations does
    rid = conn.execute("SELECT id FROM finding_relation WHERE relation_type = 'dependent'").fetchone()[0]
    review.decide_relation(conn, rid, REVIEWER_B, "rejected", note="synthetic")
    assert comparison.count_units(comparison.independence_units(conn), [a, b])["independent_units_at_most"] == 2
    review.relate(conn, a, third, "dependent", REVIEWER_A)
    review.relate(conn, third, b, "dependent", REVIEWER_A)
    assert comparison.count_units(comparison.independence_units(conn), [a, b])["independent_units_at_most"] == 1


def test_findings_of_one_experiment_form_one_unit(pair):
    project, a, b = pair
    conn = project.conn
    paper = conn.execute("SELECT paper_id FROM finding WHERE id = ?", (a,)).fetchone()[0]
    same_paper = [r["id"] for r in conn.execute(
        "SELECT id FROM finding WHERE paper_id = ? AND kind = 'measurement' ORDER BY pk", (paper,))]
    assert len(same_paper) >= 3
    exp = hierarchy.create_experiment(conn, REVIEWER_A, paper, "Synthetic run 1")
    for f in same_paper[:3]:
        hierarchy.assign(conn, f, REVIEWER_A, experiment_id=exp["id"])
    units = comparison.independence_units(conn)
    counts = comparison.count_units(units, same_paper[:3] + [b])
    assert counts["findings"] == 4 and counts["independent_units_at_most"] == 2
    assert counts["units_by_basis"] == {"experiment": 1, "dependency_relation": 0, "not_assessed": 1}
    assert units[same_paper[0]] == {"unit": f"exp:{exp['id']}", "basis": "experiment", "size": 3}


def test_comparison_table_shows_values_as_stated_and_never_a_summary(pair):
    project, a, b = pair
    conn = project.conn
    bound, per_mass = fid(conn, "0.5", "mg/L"), fid(conn, "0.05", "mg/kg")
    out = comparison.table(conn, [a, b, bound, per_mass], target_unit="g/L")
    rows = out["rows"]
    assert [r["finding_id"] for r in rows] == [a, b, bound, per_mass]         # one row per finding, nothing added
    assert [r["as_stated"] for r in rows] == ["12.5 ± 1.3 mg/L", "18 mg/L", "0.5 mg/L", "0.05 mg/kg"]
    assert (rows[0]["display_value"], rows[0]["display_unit"], rows[0]["display_rule"]) == \
        (0.0125, "g/L", "unit_conversion:mg/L->g/L")
    assert rows[1]["display_value"] == 0.018
    assert rows[2]["display_value"] is None and rows[2]["display_high"] == 0.0005      # a bound stays a bound
    assert rows[3]["display_value"] is None and rows[3]["display_rule"].startswith("not converted:")
    assert rows[0]["source_location"] == "main v1 · Release!E4 chars 0-20"
    assert "nothing is pooled, averaged or ranked" in out["note"]
    before = conn.execute("SELECT value_literal, unit_original, value_num FROM finding WHERE id = ?", (a,)).fetchone()
    assert tuple(before) == ("12.5", "mg/L", 12.5)                             # the finding itself is untouched


def test_saved_set_is_a_dated_record_and_goes_out_of_date_when_a_finding_changes(pair):
    project, a, b = pair
    conn = project.conn
    comparison.set_eligibility(conn, [a, b], "quantitative_candidate", REVIEWER_A, note="synthetic")
    saved = comparison.save_set(conn, "Outflow concentration", [a, b], REVIEWER_B, description="synthetic set")
    assert saved["classification"] == "quantitative_candidate" and saved["is_current"] is True
    assert saved["created_by"] == REVIEWER_B.name and saved["assessment"]["finding_versions"] == {a: 1, b: 1}
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE comparison_set SET classification = 'descriptive_only'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM comparison_set")

    review.edit(conn, a, REVIEWER_A, {"outcome": "peak concentration"})
    row = conn.execute("SELECT comparison_eligibility, review_status FROM finding WHERE id = ?", (a,)).fetchone()
    assert tuple(row) == ("not_assessed", "unreviewed")                        # the judgement is not carried forward
    again = comparison.get_set(conn, saved["id"])
    assert again["is_current"] is False and again["changed_since_assessment"] == [a]
    assert again["classification"] == "quantitative_candidate"                 # the stored record is unchanged
    assert comparison.assess(conn, [a, b])["blocking"] == ["verification", "outcome"]
    assert [s["id"] for s in comparison.list_sets(conn)] == [saved["id"]]
    assert audit.verify_chain(conn)[0] is True


def test_synonyms_are_grouped_only_through_an_explicit_reviewer_mapping(pair):
    project, a, b = pair
    conn = project.conn
    review.edit(conn, a, REVIEWER_A, {"outcome": "measured value"})            # differs from 'Measured value' by case
    review.verify(conn, a, REVIEWER_A, ALL_CONFIRMED, acknowledge_flags=True)
    out = check(comparison.assess(conn, [a, b]), "outcome")
    assert out["status"] == "differs" and set(out["values"]) == {"measured value", "Measured value"}

    with pytest.raises(ValidationError, match="exactly as it is stored"):
        vocabulary.set_term(conn, "outcome", "not a stored term", "concentration", REVIEWER_A)
    with pytest.raises(ValidationError, match="field must be one of"):
        vocabulary.set_term(conn, "unit_original", "mg/L", "g/L", REVIEWER_A)
    with pytest.raises(ReviewError):
        vocabulary.set_term(conn, "outcome", "measured value", "concentration", Actor("rules", "automated"))
    vocabulary.set_term(conn, "outcome", "measured value", "concentration", REVIEWER_A, note="synthetic mapping")
    assert check(comparison.assess(conn, [a, b]), "outcome")["status"] == "differs"     # only one of the two is mapped
    vocabulary.set_term(conn, "outcome", "Measured value", "concentration", REVIEWER_A)
    out = check(comparison.assess(conn, [a, b]), "outcome")
    assert out["status"] == "ok" and out["values"] == {"concentration": ["measured value", "Measured value"]}
    assert "vocabulary mapping" in out["detail"]
    stored = conn.execute("SELECT outcome FROM finding WHERE id = ?", (b,)).fetchone()[0]
    assert stored == "Measured value"                                           # original terms are never rewritten
    assert vocabulary.usage(conn, "outcome") == [
        {"original_term": "Measured value", "findings": 5, "normalized_term": "concentration"},
        {"original_term": "measured value", "findings": 1, "normalized_term": "concentration"}]

    with pytest.raises(ValidationError, match="reason"):
        vocabulary.remove_term(conn, "outcome", "measured value", REVIEWER_A, "")
    vocabulary.remove_term(conn, "outcome", "measured value", REVIEWER_A, "synthetic withdrawal")
    assert check(comparison.assess(conn, [a, b]), "outcome")["status"] == "differs"
    assert len(vocabulary.terms(conn, include_inactive=True)) == 2 and len(vocabulary.terms(conn)) == 1


def test_comparison_judgements_need_a_named_reviewer(pair):
    project, a, b = pair
    with pytest.raises(ReviewError):
        comparison.save_set(project.conn, "x", [a, b], Actor("rules", "automated"))
    with pytest.raises(ReviewError):
        comparison.set_eligibility(project.conn, [a], "descriptive_only", Actor("geodesic", "system"))
    with pytest.raises(ValidationError, match="needs a name"):
        comparison.save_set(project.conn, " ", [a, b], REVIEWER_A)
    assert OPERATOR.kind == "human"


def test_simulated_comparison_judgements_are_refused_in_a_real_project(real_project):
    with pytest.raises(ReviewError, match="only permitted in synthetic projects"):
        comparison.set_eligibility(real_project.conn, [], "descriptive_only", Actor("Simulated reviewer", "simulated"))
