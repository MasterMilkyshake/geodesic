"""Traceability of normalized and derived values, unit handling and rule-based validation."""

from __future__ import annotations

import sqlite3

import pytest

from conftest import ALL_CONFIRMED, OPERATOR, REVIEWER_A, REVIEWER_B, finding_by_wording
from geodesic import audit, findings as F, review
from geodesic.documents import store
from geodesic.errors import ValidationError
from geodesic.extraction import candidates, derived, manual, validation
from geodesic.quantities import units

TANK = "The source tank held 250 L of water and was stirred at 120 rpm."


def _promote(evidence, snap, literal, **changes):
    return candidates.promote(evidence.project, evidence.candidate(snap, literal)["id"], REVIEWER_A,
                              changes=changes or None)


def _tank_volume(evidence):
    loc = manual.build_location(evidence.conn, evidence.pdf_snap, unit_key="0", quote=TANK)
    return manual.create_finding(evidence.project, evidence.pdf_paper, REVIEWER_A, location=loc, content={
        "kind": "measurement", "statement_type": "exact", "value_literal": "250", "value_num": 250,
        "unit_original": "L", "outcome": "tank volume"})


# ------------------------------------------------------------------ traceability
def test_normalized_value_is_traceable_to_the_stored_source_text(evidence):
    conn, project = evidence.conn, evidence.project
    f = _promote(evidence, evidence.pdf_snap, "18.5 mg/L", outcome="outflow concentration")
    before = F.get_finding(conn, f["id"])
    d = derived.normalize_finding(project, f["id"], REVIEWER_A, target_unit="µg/mL")

    assert (d["kind"], d["rule_id"], d["output_value"], d["output_unit"], d["status"]) == \
        ("normalized", "unit_conversion:mg/L->µg/mL", 18.5, "µg/mL", "current")
    assert d["formula"] == "18.5 [mg/L] × 1 = [µg/mL]" and d["created_by"] == REVIEWER_A.name
    (inp,) = d["inputs"]
    assert (inp["finding_id"], inp["version_no"], inp["source_literal"], inp["unit"], inp["value"]) == \
        (f["id"], 1, "18.5", "mg/L", 18.5)

    # the original wording, literal and unit of the finding are untouched
    after = F.get_finding(conn, f["id"])
    assert after["content"] == before["content"] and after["current_version"] == 1
    assert (after["content"]["value_literal"], after["content"]["unit_original"]) == ("18.5", "mg/L")

    # the trace re-reads the stored page text at the literal's own span
    t = derived.trace(conn, d["id"])
    assert t["traceable"] is True and t["all_inputs_current"] is True
    (step,) = t["steps"]
    assert step["address"] == f"document snapshot {evidence.pdf_snap} page index 1 chars 261-307"
    assert step["literal_span"] == [297, 301] and step["stored_text_at_span"] == "18.5"
    assert step["literal_found_at_span"] and step["excerpt_resolves"] and step["problem"] is None
    assert step["source_wording"] == "The outflow concentration peaked at 18.5 mg/L."
    assert step["paper_id"] == evidence.pdf_paper
    page = store.units(conn, evidence.pdf_snap)[1]["text"]
    assert page[297:301] == inp["source_literal"]

    # asking again returns the stored value instead of a duplicate
    again = derived.normalize_finding(project, f["id"], REVIEWER_A, target_unit="µg/mL")
    assert again["already_present"] is True and again["id"] == d["id"]
    assert conn.execute("SELECT COUNT(*) FROM derived_value").fetchone()[0] == 1
    assert [r[0] for r in conn.execute("SELECT finding_id FROM derived_value_input WHERE derived_value_id = ?",
                                       (d["id"],))] == [f["id"]]


def test_derived_value_is_traceable_to_each_input(evidence):
    conn, project = evidence.conn, evidence.project
    conc = _promote(evidence, evidence.pdf_snap, "18.5 mg/L")
    tank = _tank_volume(evidence)
    d = derived.derive(project, "ratio", [tank["id"], conc["id"]], REVIEWER_A)
    assert (d["kind"], d["rule_id"], d["output_unit"]) == ("derived", "ratio", "(L)/(mg/L)")
    assert d["output_value"] == pytest.approx(250 / 18.5)
    assert d["formula"] == "250 [L] ÷ (18.5 [mg/L] × 1)"
    assert d["carries"]["uncertainty"] == "not propagated" and d["carries"]["unit_handling"] == "quotient_of_units"
    assert d["carries"]["same_paper"] is True and d["carries"]["same_experiment"] is False

    t = derived.trace(conn, d["id"])
    assert t["traceable"] is True
    a, b = t["steps"]
    assert (a["operand"], a["finding_id"], a["stored_text_at_span"], a["literal_span"]) == \
        ("a", tank["id"], "250", [257, 260])
    assert (b["operand"], b["finding_id"], b["stored_text_at_span"], b["literal_span"]) == \
        ("b", conc["id"], "18.5", [297, 301])
    assert "page index 0" in a["address"] and "page index 1" in b["address"]
    with pytest.raises(ValidationError, match="different dimensions"):
        derived.derive(project, "difference", [tank["id"], conc["id"]], REVIEWER_A)
    with pytest.raises(ValidationError, match="two different findings"):
        derived.derive(project, "ratio", [tank["id"], tank["id"]], REVIEWER_A)


def test_value_normalized_from_a_workbook_cell_traces_to_that_cell(imported):
    project, _ = imported
    conn = project.conn
    f = conn.execute("SELECT id FROM finding WHERE value_literal = '12.5' AND unit_original = 'mg/L'").fetchone()
    d = derived.normalize_finding(project, f["id"], REVIEWER_A)
    assert (d["output_value"], d["output_unit"], d["formula"]) == (0.0125, "g/L", "12.5 [mg/L] × 0.001 = [g/L]")
    # the stated uncertainty is scaled with the value and keeps its stated type
    assert d["carries"]["uncertainty"]["type"] == "sd" and d["carries"]["uncertainty"]["literal"] == "1.3"
    assert d["carries"]["uncertainty"]["value"] == pytest.approx(0.0013)
    (step,) = derived.trace(conn, d["id"])["steps"]
    assert "sheet 'Release' cell E4" in step["address"]
    assert step["literal_span"] == [0, 4] and step["stored_text_at_span"] == "12.5" and step["literal_found_at_span"]


def test_editing_a_finding_marks_its_computed_values_stale(evidence):
    conn, project = evidence.conn, evidence.project
    conc = _promote(evidence, evidence.pdf_snap, "18.5 mg/L")
    tank = _tank_volume(evidence)
    n = derived.normalize_finding(project, conc["id"], REVIEWER_A)
    r = derived.derive(project, "ratio", [tank["id"], conc["id"]], REVIEWER_A)
    other = derived.normalize_finding(project, tank["id"], REVIEWER_A, target_unit="mL")
    assert other["output_value"] == 250000.0

    review.edit(conn, conc["id"], REVIEWER_A, {"outcome": "peak outflow concentration"})
    status = {d["id"]: (d["status"], d["stale_reason"]) for d in derived.list_values(conn, include_stale=True)}
    assert status[n["id"]] == status[r["id"]] == ("stale", "finding edited by reviewer")
    assert status[other["id"]] == ("current", None)
    assert [d["id"] for d in derived.list_values(conn)] == [other["id"]]            # stale values are not listed
    t = derived.trace(conn, n["id"])
    assert t["all_inputs_current"] is False
    assert (t["steps"][0]["version_used"], t["steps"][0]["current_version"]) == (1, 2)

    # recomputing gives a new value for the new version; the stale one stays in the record
    fresh = derived.normalize_finding(project, conc["id"], REVIEWER_A)
    assert fresh["already_present"] is False and fresh["id"] != n["id"] and fresh["inputs"][0]["version_no"] == 2
    assert conn.execute("SELECT COUNT(*) FROM derived_value").fetchone()[0] == 4

    with pytest.raises(ValidationError, match="reason is required"):
        derived.withdraw(conn, other["id"], REVIEWER_A, "")
    derived.withdraw(conn, other["id"], REVIEWER_A, "not needed for the synthetic demo")
    assert derived.get(conn, other["id"])["status"] == "withdrawn"
    for sql in ("DELETE FROM derived_value", "UPDATE derived_value SET output_value = 1",
                "UPDATE derived_value SET formula = 'x'", "DELETE FROM derived_value_input"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


# ------------------------------------------------------------------ what is never converted
def test_relative_quantities_are_never_converted(evidence):
    conn, project = evidence.conn, evidence.project
    pct = _promote(evidence, evidence.pdf_snap, "62 ± 5 %")
    with pytest.raises(ValidationError, match="relative quantities are never converted automatically"):
        derived.normalize_finding(project, pct["id"], REVIEWER_A)
    for wording_literal in ("340", "2.5"):          # arbitrary units and a fold change from the workbook
        row = conn.execute("SELECT id, relative_semantics FROM finding WHERE value_literal = ? AND"
                           " relative_semantics IN ('arbitrary_units', 'fold_change')", (wording_literal,)).fetchone()
        with pytest.raises(ValidationError, match="relative quantit"):
            derived.normalize_finding(project, row["id"], REVIEWER_A)
    # two percentages are not subtracted unless unit, semantics and stated reference are identical
    other = conn.execute("SELECT id FROM finding WHERE relative_semantics = 'percent_of_counted_population'").fetchone()
    with pytest.raises(ValidationError):
        derived.derive(project, "difference", [pct["id"], other["id"]], REVIEWER_A)
    with pytest.raises(ValidationError):
        derived.derive(project, "ratio", [pct["id"], other["id"]], REVIEWER_A)
    assert conn.execute("SELECT COUNT(*) FROM derived_value").fetchone()[0] == 0
    # the timepoint of a relative quantity is an ordinary duration and can be converted
    t = derived.normalize_finding(project, pct["id"], REVIEWER_A, field="timepoint", target_unit="h")
    assert (t["output_value"], t["output_unit"], t["inputs"][0]["source_literal"]) == (0.5, "h", "30 min")


def test_ranges_and_bounds_keep_their_form_and_ambiguous_units_are_refused(evidence):
    conn, project = evidence.conn, evidence.project
    rng = conn.execute("SELECT id FROM finding WHERE value_literal = '0.8–1.2' AND unit_original = '1/h'").fetchone()
    d = derived.normalize_finding(project, rng["id"], REVIEWER_A)
    assert d["output_value"] is None and d["carries"]["statement_type"] == "range"       # no midpoint is invented
    assert (d["output_low"], d["output_high"]) == (pytest.approx(0.8 / 3600), pytest.approx(1.2 / 3600))
    bound = conn.execute("SELECT id FROM finding WHERE value_literal = '0.05' AND unit_original = 'mg/kg'").fetchone()
    b = derived.normalize_finding(project, bound["id"], REVIEWER_A)
    assert (b["output_value"], b["output_low"], b["output_high"]) == (None, None, pytest.approx(5e-08))
    assert b["carries"]["bound_type"] == "lt" and b["carries"]["qualifiers"] == ["<"]
    with pytest.raises(ValidationError, match="only defined for point values"):
        derived.derive(project, "ratio", [rng["id"], bound["id"]], REVIEWER_A)

    amb = _promote(evidence, evidence.xml_snap, "12 m")                 # 'm' is flagged ambiguous by the parser
    assert "unit_ambiguous" in amb["review_flags"]
    with pytest.raises(ValidationError, match="Not converted"):
        derived.normalize_finding(project, amb["id"], REVIEWER_A)
    conc = _promote(evidence, evidence.pdf_snap, "18.5 mg/L")
    with pytest.raises(ValidationError, match="different dimensions"):
        derived.normalize_finding(project, conc["id"], REVIEWER_A, target_unit="L")
    with pytest.raises(ValidationError, match="field must be"):
        derived.normalize_finding(project, conc["id"], REVIEWER_A, field="sample_size")


def test_unit_table_is_explicit_and_refuses_unlisted_conversions():
    factor, rule = units.conversion("mg/L", "g/L")
    assert factor == pytest.approx(0.001) and rule == "unit_conversion:mg/L->g/L"
    assert units.conversion("h", "min")[0] == pytest.approx(60)
    for src, dst in (("%", "mg/L"), ("mg/L", "L"), ("furlong", "m"), ("fold", "%")):
        with pytest.raises(units.ConversionError):
            units.conversion(src, dst)
    table = units.unit_table_rows()
    assert all({"symbol", "dimension", "factor_to_base", "base_unit", "ambiguous"} <= set(row) for row in table)
    assert any(row["symbol"] == "mg/L" for row in table)


# ------------------------------------------------------------------ validation
def test_validation_run_reports_issues_without_changing_findings(imported):
    project, _ = imported
    conn = project.conn
    before = conn.execute("SELECT id, review_status, grounding_status, current_version FROM finding ORDER BY pk").fetchall()
    out = validation.run_validation(project, OPERATOR)
    assert (out["n_findings"], out["n_issues"], out["findings_with_errors"]) == (32, 11, 0)
    assert out["by_severity"] == {"error": 0, "warning": 2, "info": 9}
    assert out["by_code"] == {"open_ambiguity_flags": 1, "paper_unresolved": 6, "relative_without_reference": 2,
                              "uncertainty_type_unspecified": 1, "unparsed_content": 1}
    assert "does not verify that findings represent their sources correctly" in out["statement"]
    assert [tuple(r) for r in conn.execute(
        "SELECT id, review_status, grounding_status, current_version FROM finding ORDER BY pk")] == \
        [tuple(r) for r in before]
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE review_status = 'verified'").fetchone()[0] == 0
    assert set(out["by_code"]) <= set(validation.SEVERITY)
    issue = validation.issues(conn, code="relative_without_reference")[0]
    assert issue["severity"] == "warning" and issue["field"] == "reference_definition"
    assert len(validation.issues(conn, severity="info")) == 9


def test_validation_detects_inconsistent_states(imported):
    """Inconsistent states are produced here by writing to the database directly, as a damaged file would."""
    project, _ = imported
    conn = project.conn
    exact = [r["id"] for r in conn.execute(
        "SELECT id FROM finding WHERE statement_type = 'exact' AND kind = 'measurement' ORDER BY pk LIMIT 2")]
    conn.execute("UPDATE finding SET review_status = 'verified' WHERE id = ?", (exact[0],))     # no review exists
    edited = review.edit(conn, exact[1], REVIEWER_A, {"value_literal": "999", "value_num": 999.0})
    assert edited["grounding_status"] == "unsupported"
    first = validation.run_validation(project, OPERATOR)
    assert first["by_code"]["unsupported"] == 1 and first["by_code"]["verified_without_current_review"] == 1

    conn.execute("UPDATE finding SET grounding_status = 'grounded' WHERE id = ?", (exact[1],))
    rng = conn.execute("SELECT id, value_low, value_high FROM finding WHERE statement_type = 'range' ORDER BY pk").fetchone()
    conn.execute("UPDATE finding SET value_low = ?, value_high = ? WHERE id = ?",
                 (rng["value_high"], rng["value_low"], rng["id"]))
    second = validation.run_validation(project, OPERATOR)
    assert second["findings_with_errors"] == 2 and second["by_severity"]["error"] == 3
    codes = {(i["finding_id"], i["code"]) for i in validation.issues(conn) if i["severity"] != "info"}
    assert {(exact[0], "verified_without_current_review"), (exact[1], "grounding_status_inconsistent"),
            (exact[1], "literal_not_at_span"), (rng["id"], "range_not_ascending")} <= codes

    # runs are kept: the earlier run still says what was known then
    assert conn.execute("SELECT COUNT(*) FROM validation_run").fetchone()[0] == 2
    assert validation.latest_run(conn)["id"] == second["run_id"]
    assert validation.summary(conn, first["run_id"])["n_issues"] == first["n_issues"]
    assert validation.summary(conn)["run_id"] == second["run_id"]
    for sql in ("DELETE FROM validation_issue", "DELETE FROM validation_run",
                "UPDATE validation_issue SET severity = 'info'", "UPDATE validation_run SET n_issues = 0"):
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)


def test_validation_flags_findings_whose_document_was_removed_and_stale_computed_values(evidence):
    conn, project = evidence.conn, evidence.project
    conc = _promote(evidence, evidence.pdf_snap, "18.5 mg/L", outcome="outflow concentration")
    review.verify(conn, conc["id"], REVIEWER_B, ALL_CONFIRMED)
    cell = _promote(evidence, evidence.xml_snap, "23 ± 2", outcome="transit time")
    derived.normalize_finding(project, conc["id"], REVIEWER_A)
    clean = validation.run_validation(project, OPERATOR)
    assert clean["findings_with_errors"] == 0
    assert {i["code"] for i in validation.issues(conn, finding_id=cell["id"])} == \
        {"uncertainty_type_unspecified", "no_stage"}

    review.edit(conn, conc["id"], REVIEWER_A, {"outcome": "peak outflow concentration"})
    res = store.remove_document(conn, evidence.pdf_doc, REVIEWER_A, "Attached to the wrong record (synthetic).")
    assert res["findings_citing_document"] == 1
    after = validation.run_validation(project, OPERATOR)
    found = {(i["code"], i["severity"]) for i in validation.issues(conn, finding_id=conc["id"])}
    assert ("source_document_removed", "error") in found and ("derived_value_stale", "warning") in found
    assert after["findings_with_errors"] == 1
    # the finding itself is kept, with its history
    got = F.get_finding(conn, conc["id"])
    assert got["record_status"] == "active" and got["current_version"] == 2
    assert audit.verify_chain(conn)[0] is True


def test_no_validation_run_is_reported_as_such(project):
    out = validation.summary(project.conn)
    assert out["run_id"] is None and "No validation run" in out["statement"]
    assert validation.latest_run(project.conn) is None and validation.issues(project.conn) == []


def test_assigning_a_verified_finding_to_an_experiment_keeps_its_verification_valid(evidence):
    """Assignment creates a new version but leaves the verified evidence unchanged; a content edit does not."""
    from geodesic.extraction import hierarchy

    conn = evidence.conn
    f = _tank_volume(evidence)
    review.verify(conn, f["id"], REVIEWER_B, ALL_CONFIRMED, acknowledge_flags=True)
    exp = hierarchy.create_experiment(conn, REVIEWER_A, evidence.pdf_paper, "Synthetic run series")
    out = hierarchy.assign(conn, f["id"], REVIEWER_A, experiment_id=exp["id"])
    row = conn.execute("SELECT review_status, current_version FROM finding WHERE id = ?", (f["id"],)).fetchone()
    assert row["review_status"] == "verified" and row["current_version"] == out["version_no"] == 2
    run = validation.run_validation(evidence.project, OPERATOR)
    assert "verified_without_current_review" not in run["by_code"]

    # the same state reached without any verification of that evidence is still reported
    review.edit(conn, f["id"], REVIEWER_A, {"outcome": "tank volume (nominal)"})
    assert conn.execute("SELECT review_status FROM finding WHERE id = ?", (f["id"],)).fetchone()[0] == "unreviewed"
    conn.execute("UPDATE finding SET review_status = 'verified' WHERE id = ?", (f["id"],))   # as a damaged file would
    run = validation.run_validation(evidence.project, OPERATOR)
    assert run["by_code"]["verified_without_current_review"] == 1
