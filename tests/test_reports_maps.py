"""Evidence maps, missing-information summary, full-text summary and the modelling inventory (synthetic data)."""

from __future__ import annotations

import pytest

from conftest import ALL_CONFIRMED, REVIEWER_A
from geodesic import query, review, vocabulary
from geodesic.errors import ValidationError
from geodesic.extraction import hierarchy
from geodesic.reports import maps, missing
from geodesic.screening import assess as assess_paper


def test_stage_by_system_map_counts_findings_of_the_fixture_workbook(imported):
    project, _ = imported
    m = maps.evidence_map(project.conn, "system_context")
    assert [(r["key"], r["findings"], r["reports"]) for r in m["rows"]] == [
        ("release", 11, 3), ("transit", 8, 4), ("retention", 7, 2), ("discharge", 6, 3)]
    assert [r["label"] for r in m["rows"]] == ["Release", "Transit", "Retention", "Discharge"]      # from configuration
    assert m["total"] == {"findings": 32, "reports": 8, "independent_units_at_most": 32, "verified": 0,
                          "quantitative": 20, "quantitative_candidates": 0}
    assert len(m["columns"]) == 9 and all(c["kind"] == "term" for c in m["columns"])
    assert maps.cell(m, "release", "Stirred source tank, 200 L") == 6
    assert maps.cell(m, "discharge", "Stirred source tank, 200 L") == 0                             # empty cell
    assert sum(c["findings"] for c in m["columns"]) == 32
    text = maps.format_map(m)
    assert "does not mean that no evidence exists" in text and "all stages" in text
    with pytest.raises(ValidationError, match="can be drawn by one of"):
        maps.evidence_map(project.conn, "unit_original")


def test_stage_by_outcome_map_keeps_unrecorded_outcomes_visible(imported):
    project, _ = imported
    m = maps.evidence_map(project.conn, "outcome")
    assert [(c["label"], c["kind"], c["findings"]) for c in m["columns"]] == [
        ("Measured value", "term", 6), (maps.NOT_RECORDED, "not_recorded", 26)]
    table = maps.map_table(m)
    assert table.name == "evidence_map_outcome" and len(table.rows) == 8
    assert {r["column_kind"] for r in table.rows} == {"term", "not_recorded"}


def test_finding_in_two_stages_counts_in_both_rows_and_once_in_totals(imported):
    project, _ = imported
    conn = project.conn
    fid = conn.execute("SELECT f.id FROM finding f JOIN finding_stage fs ON fs.finding_id = f.id JOIN stage s"
                       " ON s.id = fs.stage_id WHERE s.key = 'discharge' ORDER BY f.pk").fetchone()[0]
    review.assign_stages(conn, fid, REVIEWER_A, add=["transit"])
    m = maps.evidence_map(conn, "system_context")
    rows = {r["key"]: r["findings"] for r in m["rows"]}
    assert rows == {"release": 11, "transit": 9, "retention": 7, "discharge": 6}
    assert m["total"]["findings"] == 32 and m["findings_in_several_stages"] == 1
    assert maps.cell(m, "transit", "Outflow weir") == 1 and maps.cell(m, "discharge", "Outflow weir") == 6
    assert next(c for c in m["columns"] if c["key"] == "Outflow weir")["findings"] == 6


def test_rare_terms_are_folded_into_a_labelled_other_column(imported):
    project, _ = imported
    m = maps.evidence_map(project.conn, "system_context", max_columns=3)
    assert [c["label"] for c in m["columns"]] == [
        "Outflow weir", "Stirred source tank, 200 L", "Holding reservoir A", "other (6 terms)"]
    other = m["columns"][-1]
    assert other["kind"] == "other" and len(other["original_terms"]) == 6 and other["findings"] == 15
    assert sum(c["findings"] for c in m["columns"]) == 32
    assert any("least frequent terms" in n for n in m["notes"])


def test_map_groups_terms_only_through_reviewer_mapping_and_counts_units(imported):
    project, _ = imported
    conn = project.conn
    before = maps.evidence_map(conn, "system_context")
    assert maps.cell(before, "release", "Lined source tank") == 4 and maps.cell(before, "release", "Pilot tank") == 1
    vocabulary.set_term(conn, "system_context", "Lined source tank", "source tank", REVIEWER_A)
    vocabulary.set_term(conn, "system_context", "Pilot tank", "source tank", REVIEWER_A)
    m = maps.evidence_map(conn, "system_context")
    col = next(c for c in m["columns"] if c["key"] == "source tank")
    assert col["kind"] == "mapped" and col["original_terms"] == ["Lined source tank", "Pilot tank"]
    assert col["findings"] == 5 and maps.cell(m, "release", "source tank") == 5
    assert "'source tank' groups (reviewer mapping): Lined source tank; Pilot tank" in maps.format_map(m)
    # 'Stirred source tank, 200 L' is a different stored term and stays its own column
    assert maps.cell(m, "release", "Stirred source tank, 200 L") == 6

    # findings of one experiment are one unit; verified findings are counted as such
    rows = [r["id"] for r in conn.execute(
        "SELECT id FROM finding WHERE system_context = 'Stirred source tank, 200 L' AND kind = 'measurement' ORDER BY pk")]
    paper = conn.execute("SELECT paper_id FROM finding WHERE id = ?", (rows[0],)).fetchone()[0]
    exp = hierarchy.create_experiment(conn, REVIEWER_A, paper, "Synthetic run")
    for f in rows:
        hierarchy.assign(conn, f, REVIEWER_A, experiment_id=exp["id"])
    review.verify(conn, rows[0], REVIEWER_A, ALL_CONFIRMED, acknowledge_flags=True)
    m = maps.evidence_map(conn, "system_context")
    c = m["cells"]["release"]["Stirred source tank, 200 L"]
    assert c["findings"] == 6 and c["independent_units_at_most"] == 6 - len(rows) + 1 and c["verified"] == 1
    assert m["total"]["independent_units_at_most"] == 32 - len(rows) + 1


def test_map_respects_the_finding_filter(imported):
    project, _ = imported
    m = maps.evidence_map(project.conn, "system_context", flt=query.FindingFilter(kinds=["measurement"]))
    assert m["total"]["findings"] == 20 and m["total"]["quantitative"] == 20
    m = maps.evidence_map(project.conn, "system_context", flt=query.FindingFilter(stage_keys=["release"]))
    assert [r["findings"] for r in m["rows"]] == [11, 0, 0, 0]


def test_missing_information_keeps_reasons_apart(imported):
    project, _ = imported
    conn = project.conn
    s = missing.missing_information(conn)
    rows = {(r["stage_key"], r["field"]): r for r in s["rows"]}
    overall = rows[(missing.ALL_STAGES, "outcome")]
    assert (overall["findings"], overall["recorded"], overall["not_yet_extracted"], overall["not_reported"]) == (20, 5, 15, 0)
    assert rows[(missing.ALL_STAGES, "denominator")]["no_status_recorded"] == 20
    assert rows[(missing.ALL_STAGES, "reference_definition")]["findings"] == 9          # relative quantities only
    assert rows[("release", "timepoint")]["recorded"] == 2
    for r in s["rows"]:
        assert r["findings"] == sum(r[state] for state in missing.STATES)

    # a reviewer records that the source does not report the sample size: it moves, nothing else does
    fid = conn.execute("SELECT id FROM finding WHERE kind = 'measurement' AND sample_size IS NULL ORDER BY pk").fetchone()[0]
    status = dict(review_field_status(conn, fid), sample_size="not_reported")
    review.edit(conn, fid, REVIEWER_A, {"field_status": status})
    after = {(r["stage_key"], r["field"]): r for r in missing.missing_information(conn)["rows"]}
    a, b = rows[(missing.ALL_STAGES, "sample_size")], after[(missing.ALL_STAGES, "sample_size")]
    assert b["not_reported"] == a["not_reported"] + 1 and b["not_yet_extracted"] == a["not_yet_extracted"] - 1
    assert b["recorded"] == a["recorded"]
    table = missing.missing_table(s)
    assert table.columns[:5] == ["stage_key", "stage", "field", "population", "findings"]
    assert "no_status_recorded" in missing.format_missing(s) or "no status" in missing.format_missing(s)


def review_field_status(conn, fid):
    import json

    return json.loads(conn.execute("SELECT field_status_json FROM finding WHERE id = ?", (fid,)).fetchone()[0])


def test_fulltext_summary_and_modelling_inventory_are_inventories(corpus):
    conn = corpus.conn
    ft = missing.fulltext_summary(conn)
    assert ft["reports"] == 18 and ft["by_availability"]["not_checked"] == 17
    assert ft["by_availability"]["open_access_reported"] == 1 and ft["reports_with_findings"] == 8
    inv = missing.modeling_inventory(conn)
    overall = inv["rows"][0]
    assert overall["stage_key"] == "(all stages)" and overall["quantitative_findings"] == 20
    assert overall["with_timepoint"] == 7 and overall["absolute_quantities"] == 11 and overall["verified"] == 0
    assert [r["stage_key"] for r in inv["rows"][1:]] == ["release", "transit", "retention", "discharge"]
    assert "No model is fitted" in inv["statement"]
    assert inv["reviewer_assessments"] == {"quantitative_comparison": {}, "mathematical_modeling": {}}
    paper = conn.execute("SELECT id FROM paper WHERE origin = 'workbook' ORDER BY pk").fetchone()[0]
    assess_paper(conn, paper, "mathematical_modeling", "limited", REVIEWER_A, rationale="synthetic")
    inv = missing.modeling_inventory(conn)
    assert inv["reviewer_assessments"]["mathematical_modeling"] == {"limited": 1}
    assert "limited: 1 paper(s)" in missing.format_modeling(inv)


def test_time_series_candidates_need_one_experiment_outcome_and_unit(imported):
    project, _ = imported
    conn = project.conn
    assert missing.modeling_inventory(conn)["rows"][0]["time_series_candidates"] == 0
    timed = [dict(r) for r in conn.execute(
        "SELECT id, paper_id, outcome, unit_original, timepoint_value, time_unit FROM finding WHERE kind = 'measurement'"
        " AND timepoint_value IS NOT NULL AND value_num IS NOT NULL ORDER BY paper_id, pk")]
    assert timed, "the fixture workbook has findings with a time point"
    # the inventory counts only what is recorded: no synthetic series exists in the fixture, so it stays 0
    by_paper: dict = {}
    for t in timed:
        by_paper.setdefault(t["paper_id"], []).append(t)
    paper, items = max(by_paper.items(), key=lambda kv: len(kv[1]))
    exp = hierarchy.create_experiment(conn, REVIEWER_A, paper, "Synthetic series")
    for t in items:
        hierarchy.assign(conn, t["id"], REVIEWER_A, experiment_id=exp["id"])
    groups: dict = {}
    for t in items:
        groups.setdefault((t["outcome"], t["unit_original"]), set()).add((t["timepoint_value"], t["time_unit"]))
    expected = sum(1 for v in groups.values() if len(v) >= 3)
    assert missing.modeling_inventory(conn)["rows"][0]["time_series_candidates"] == expected
