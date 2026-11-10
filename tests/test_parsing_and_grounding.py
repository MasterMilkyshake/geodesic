"""Deterministic narrative parsing, grounding rules, units and the extraction schema."""

from __future__ import annotations

import pytest

from conftest import OPERATOR, finding_by_wording
from geodesic import findings as F
from geodesic.errors import ValidationError
from geodesic.quantities import units
from geodesic.textparse import check_transcription, number_from_literal, parse_text, split_segments
from geodesic.util import loads
from geodesic.workbook.importer import import_workbook


def one(text: str):
    p = parse_text(text)
    assert len(p.quantities) == 1, [q.to_dict() for q in p.quantities]
    return p, p.quantities[0]


# ------------------------------------------------------------------ segments and spans
def test_segments_carry_character_spans_into_the_cell_text():
    text = ("Discharge cleared 90 % of the tracer within 72 h. Clearance rate 0.12 ± 0.02 1/h (SE), n = 5 reservoirs. "
            "Signal was 340 a.u. (95% CI 300-380), e.g. in run 2; done.")
    segs = split_segments(text)
    assert [s.text for s in segs] == [
        "Discharge cleared 90 % of the tracer within 72 h.",
        "Clearance rate 0.12 ± 0.02 1/h (SE), n = 5 reservoirs.",
        "Signal was 340 a.u. (95% CI 300-380), e.g. in run 2",
        "done."]
    for s in segs:
        assert text[s.start:s.end] == s.text                      # 0-based, half-open offsets
    assert [s.text for s in split_segments("a; b", on_semicolon=False)] == ["a; b"]


def test_every_parsed_literal_is_the_source_text_at_its_span():
    text = "Tracer release was approximately 42 % of the loaded mass at 24 h (n = 6 runs)"
    p, q = one(text)
    assert text[q.value_start:q.value_end] == q.value_literal == "42"
    assert text[q.unit_start:q.unit_end] == q.unit_literal == "%"
    assert text[q.reference_start:q.reference_end] == q.reference_definition == "loaded mass"
    t = p.timepoints[0]
    assert text[t.value_start:t.unit_end] == "24 h" and t.value_num == 24
    n = p.sample_sizes[0]
    assert text[n.start:n.end] == n.literal == "n = 6 runs" and n.value == 6 and n.replicate_type == "runs"
    assert check_transcription(text, q.value_start, q.value_end, "42")
    assert not check_transcription(text, q.value_start, q.value_end, "24")
    assert not check_transcription(text, -1, 2, "Tr") and not check_transcription(text, 0, 10_000, text)


# ------------------------------------------------------------------ qualifiers, bounds, ranges, uncertainty
def test_approximate_values_keep_their_qualifier():
    _, q = one("Tracer release was approximately 42 % of the loaded mass")
    assert q.statement_type == "approximate" and q.value_num == 42
    assert [x["text"] for x in q.qualifiers] == ["approximately"]
    _, q = one("Retained fraction ~ 15 % (n = 3).")
    assert q.statement_type == "approximate" and q.qualifiers[0]["text"] == "~"


@pytest.mark.parametrize("text, bound, low, high", [
    ("Release rate stayed > 5 mL/min throughout.", "gt", 5.0, None),
    ("Flow of at least 30 L/min was required", "ge", 30.0, None),
    ("< 0.05 mg/kg", "lt", None, 0.05),
    ("Residual concentration ≤ 0.5 mg/L after 10 days.", "le", None, 0.5),
])
def test_bounds_are_not_point_estimates(text, bound, low, high):
    _, q = one(text)
    assert q.statement_type == "bound" and q.bound_type == bound
    assert q.value_num is None                                   # never reduced to a point value
    assert (q.value_low, q.value_high) == (low, high)


@pytest.mark.parametrize("text, literal, low, high", [
    ("Between 10 and 20 % of the tracer remained bound to the liner", "10 and 20", 10.0, 20.0),
    ("0.8–1.2 1/h", "0.8–1.2", 0.8, 1.2),
    ("values approximately 7 to 9 L/min.", "7 to 9", 7.0, 9.0),
])
def test_ranges_are_not_point_estimates(text, literal, low, high):
    _, q = one(text)
    assert q.statement_type == "range" and q.value_literal == literal
    assert q.value_num is None and (q.value_low, q.value_high) == (low, high)


def test_approximate_range_and_from_to_are_flagged():
    _, q = one("values approximately 7 to 9 L/min.")
    assert "approximate_range" in q.flags
    _, q = one("Dispersion increased from 2 to 8 mL/min with flow rate.")
    assert "from_to_may_be_change_not_range" in q.flags


def test_plus_minus_is_not_assumed_to_be_sd_or_se():
    _, q = one("Release half-time was 4.5 h ± 0.5.")
    assert q.uncertainty_type == "unspecified_plus_minus" and q.uncertainty_value == 0.5
    assert "uncertainty_type_unspecified" in q.flags
    _, q = one("12.5 ± 1.3 mg/L (SD)")
    assert q.uncertainty_type == "sd" and "uncertainty_type_unspecified" not in q.flags
    _, q = one("Clearance rate 0.12 ± 0.02 1/h (SE), n = 5 reservoirs.")
    assert q.uncertainty_type == "se"
    _, q = one("Transit signal was 340 a.u. (95% CI 300-380), mean of 4 runs.")
    assert (q.uncertainty_type, q.uncertainty_low, q.uncertainty_high, q.confidence_level) == ("ci", 300, 380, 95)
    assert q.uncertainty_value is None


def test_missing_uncertainty_stays_missing():
    _, q = one("Level was 18 mg/L")
    assert q.uncertainty_type is None and q.uncertainty_literal is None and q.uncertainty_value is None


def test_count_phrase_sample_size_is_marked_for_review():
    p, _ = one("Transit signal was 340 a.u. (95% CI 300-380), mean of 4 runs.")
    assert p.sample_sizes[0].from_count_phrase is True
    assert p.summary_statistic["name"] == "mean"


def test_numbers_that_are_not_measurements_are_not_extracted():
    p = parse_text("p < 0.05 for the difference; see doi 10.5555/geodesic.synth.001 (2019).")
    assert p.quantities == []
    assert [u["reason"] for u in p.unparsed] == ["year_like_number"]


def test_ambiguous_decimal_comma_is_not_guessed():
    p = parse_text("1,5 mg")
    assert p.quantities == []
    assert p.unparsed == [{"start": 0, "end": 3, "text": "1,5", "reason": "ambiguous_decimal_comma"}]


def test_numeric_literals_parse_exactly():
    assert number_from_literal("1.2 × 10^3") == 1200
    assert number_from_literal("3e-2") == 0.03
    assert number_from_literal("1,200") == 1200
    with pytest.raises(ValueError):
        number_from_literal("about five")


# ------------------------------------------------------------------ relative quantities
@pytest.mark.parametrize("text, semantics", [
    ("Recovery was 85 % of control.", "percent_of_reference"),
    ("Retention was 65 % of inflow per litre at 12 h", "percent_of_reference_per_mass_or_volume"),
    ("2.5-fold higher than baseline at day 7", "fold_change"),
    ("30 % of counted particles were retained.", "percent_of_counted_population"),
    ("Transit signal was 340 a.u. in the conduit", "arbitrary_units"),
    ("Retained fraction ~ 15 %", "unspecified_relative"),
    ("Level was 18 mg/L", "absolute"),
])
def test_relative_quantities_have_explicit_semantics(text, semantics):
    _, q = one(text)
    assert q.relative_semantics == semantics


def test_relative_quantities_are_never_converted():
    for a, b in (("%", "-fold"), ("-fold", "%"), ("a.u.", "mg/L"), ("%", "mg/L")):
        with pytest.raises(units.ConversionError, match="never converted automatically|different dimensions|not in"):
            units.conversion(a, b)
    with pytest.raises(units.ConversionError, match="never converted automatically"):
        units.conversion("%", "%")


def test_unit_conversion_table_and_unsupported_or_ambiguous_units():
    factor, rule = units.conversion("mg/L", "g/L")
    assert factor == pytest.approx(1e-3) and rule == "unit_conversion:mg/L->g/L"
    factor, _ = units.conversion("h", "min")
    assert factor == pytest.approx(60)
    assert units.lookup("furlongs")[0] == "unrecognized"
    assert units.lookup(None)[0] == "missing" and units.lookup("  ")[0] == "missing"
    assert units.lookup("M")[0] == "ambiguous" and units.lookup("m")[0] == "ambiguous"
    assert units.lookup("widgets", {"widgets": "count"})[0] == "project_defined"
    with pytest.raises(units.ConversionError, match="not in the conversion table"):
        units.conversion("furlongs", "m")
    with pytest.raises(units.ConversionError, match="ambiguous"):
        units.conversion("M", "mmol/L")
    with pytest.raises(units.ConversionError, match="different dimensions"):
        units.conversion("mg/L", "h")
    # in text: an unknown unit is flagged, an ambiguous one is flagged, neither is silently normalised
    _, q = one("Level was 12 furlongs.")
    assert q.unit_literal is None and "no_unit_detected" in q.flags
    p = parse_text("5 M and 3 m")
    assert [q.unit_ambiguous for q in p.quantities] == [True, True]
    assert all("unit_ambiguous" in q.flags for q in p.quantities)


# ------------------------------------------------------------------ imported findings
def test_narrative_cell_with_several_facts_becomes_several_grounded_findings(imported):
    project, _ = imported
    conn = project.conn
    cell_findings = conn.execute(
        "SELECT f.*, l.char_start, l.char_end FROM finding f JOIN source_location l ON l.owner_id = f.id AND"
        " l.role = 'supporting_excerpt' WHERE l.sheet = 'Release' AND l.cell_ref = 'D4' ORDER BY l.char_start"
    ).fetchall()
    assert len(cell_findings) == 2
    a, b = cell_findings
    assert a["statement_type"] == "approximate" and a["value_literal"] == "42" and a["value_num"] == 42
    assert a["timepoint_literal"] == "24 h" and a["timepoint_value"] == 24 and a["time_unit"] == "h"
    assert a["sample_size"] == 6 and a["sample_size_literal"] == "n = 6 runs" and a["replicate_type"] == "runs"
    assert a["relative_semantics"] == "percent_of_reference" and a["reference_definition"] == "loaded mass"
    quals = [q["text"] for q in loads(a["qualifiers_json"], [])]
    assert "approximately" in quals and "denominator unclear" in quals
    assert a["uncertainty_type"] is None                          # nothing invented
    assert loads(a["field_status_json"], {}).get("uncertainty") in ("not_yet_extracted", "not_reported")
    assert b["statement_type"] == "bound" and b["bound_type"] == "gt" and b["value_num"] is None
    assert b["value_low"] == 5 and b["unit_original"] == "mL/min"
    assert a["char_end"] <= b["char_start"]                       # distinct, ordered spans in the same cell


def test_qualifier_phrases_from_limitations_are_preserved(imported):
    project, _ = imported
    lim = finding_by_wording(project.conn, "not independently verified", kind="limitation")
    assert lim["statement_type"] == "qualitative"
    assert "not independently verified" in [q["text"] for q in loads(lim["qualifiers_json"], [])]
    # the limitation is linked to the findings of its row and repeated in their interpretation limits
    n = project.conn.execute(
        "SELECT COUNT(*) FROM finding_relation WHERE relation_type = 'limited_by' AND finding_b = ?", (lim["id"],)
    ).fetchone()[0] + project.conn.execute(
        "SELECT COUNT(*) FROM finding_relation WHERE relation_type = 'limited_by' AND finding_a = ?", (lim["id"],)
    ).fetchone()[0]
    assert n >= 1
    f = finding_by_wording(project.conn, "Release rate stayed > 5 mL/min")
    assert "not independently verified" in f["interpretation_limits"]


def test_narrative_summaries_are_kept_apart_from_structured_measurements(imported):
    project, _ = imported
    conn = project.conn
    q = finding_by_wording(conn, "Release appeared faster at higher stirring speed")
    assert (q["kind"], q["statement_type"], q["value_literal"], q["value_num"]) == ("narrative", "qualitative",
                                                                                   None, None)
    m = finding_by_wording(conn, "12.5 ± 1.3 mg/L (SD)")
    assert (m["kind"], m["value_num"], m["unit_original"], m["uncertainty_type"], m["uncertainty_value"]) == (
        "measurement", 12.5, "mg/L", "sd", 1.3)
    t = finding_by_wording(conn, "Release was complete by day 7")
    assert t["kind"] == "narrative" and t["timepoint_literal"] == "day 7" and t["timepoint_value"] == 7
    assert t["value_num"] is None
    unparsed = conn.execute("SELECT * FROM finding WHERE statement_type = 'unparsed'").fetchall()
    assert len(unparsed) == 1 and unparsed[0]["source_wording"] == "1,5 mg" and unparsed[0]["value_num"] is None
    draft = conn.execute("SELECT * FROM finding WHERE review_status = 'draft'").fetchall()
    assert len(draft) == 1 and "from 2 to 8" in draft[0]["source_wording"]


def test_field_status_distinguishes_kinds_of_missing(imported):
    project, _ = imported
    seen = set()
    for r in project.conn.execute("SELECT field_status_json FROM finding"):
        seen |= set(loads(r[0], {}).values())
    # an import cannot know that a source did not report something: it only says what it has not extracted
    assert seen == {"not_yet_extracted"}
    assert F.FIELD_STATUSES == ("not_reported", "not_applicable", "not_yet_extracted", "could_not_be_parsed")
    for status in F.FIELD_STATUSES:
        assert F.validate_content(_valid(field_status={"uncertainty": status}))["field_status"] == {
            "uncertainty": status}


# ------------------------------------------------------------------ grounding of proposed transcriptions
def test_proposal_whose_literal_is_not_at_its_span_is_rejected_and_logged(project, workbook_v1, mapping):
    """An external proposer (stand-in for any non-deterministic extractor) claims values the text does not contain."""
    calls = []

    def proposer(cell_text: str, start: int, end: int):
        seg = cell_text[start:end]
        calls.append(seg)
        if "Release appeared faster" not in seg:
            return []
        good = {"content": F.blank_content(kind="measurement", source_wording=seg, statement_type="exact",
                                           value_literal="higher", value_num=1.0),
                "spans": {"value_literal": [start + seg.index("higher"), start + seg.index("higher") + 6]}}
        bad = {"content": F.blank_content(kind="measurement", source_wording=seg, statement_type="exact",
                                          value_literal="37", value_num=37.0, unit_original="mg/L"),
               "spans": {"value_literal": [start + 2, start + 4], "unit_original": [start + 5, start + 9]}}
        no_span = {"content": F.blank_content(kind="measurement", source_wording=seg, statement_type="exact",
                                              value_literal="12", value_num=12.0), "spans": {}}
        return [bad, no_span, good]

    result = import_workbook(project, workbook_v1, mapping, actor=OPERATOR, proposer=proposer)
    conn = project.conn
    assert calls, "the proposer was consulted"
    failures = [dict(r) for r in conn.execute("SELECT * FROM parse_failure ORDER BY id")]
    assert result.stats["parse_failures"] == len(failures) >= 2
    claimed = {f["claimed_literal"] for f in failures}
    assert {"37", "12"} <= claimed
    for f in failures:
        assert f["sheet"] == "Release" and f["cell_ref"] and f["reason"]
    # nothing with the fabricated values was stored
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE value_literal IN ('37', '12')").fetchone()[0] == 0
    # the grounded proposal was accepted, but only as a draft that a reviewer must check
    accepted = conn.execute("SELECT * FROM finding WHERE value_literal = 'higher'").fetchall()
    assert len(accepted) == 1 and accepted[0]["review_status"] == "draft"
    assert accepted[0]["extraction_method"] == "external_proposer"
    assert "proposed_by_external_proposer" in loads(accepted[0]["review_flags_json"], [])
    # other findings still name the deterministic parser and its version
    methods = {r[0] for r in conn.execute("SELECT DISTINCT extraction_method FROM finding")}
    assert methods == {"external_proposer", "geodesic-deterministic-parser/1.0"}


def test_segment_whose_proposals_are_all_rejected_is_kept_as_an_unparsed_draft(project, workbook_v1, mapping):
    def proposer(cell_text: str, start: int, end: int):
        seg = cell_text[start:end]
        if "Release appeared faster" not in seg:
            return []
        return [{"content": F.blank_content(kind="measurement", source_wording=seg, statement_type="exact",
                                            value_literal="99", value_num=99.0),
                 "spans": {"value_literal": [start, start + 2]}}]

    import_workbook(project, workbook_v1, mapping, actor=OPERATOR, proposer=proposer)
    f = finding_by_wording(project.conn, "Release appeared faster")
    assert (f["statement_type"], f["review_status"], f["value_literal"], f["value_num"]) == ("unparsed", "draft",
                                                                                           None, None)
    assert "transcription_rejected" in loads(f["review_flags_json"], [])
    failure = project.conn.execute("SELECT * FROM parse_failure").fetchall()
    assert len(failure) == 1 and failure[0]["claimed_literal"] == "99"
    assert failure[0]["reason"] == "claimed source string is absent from the specified span"


def test_grounding_problems_reports_each_ungrounded_literal():
    text = "Level was 18 mg/L at 2 h"
    locs = [{"role": "supporting_excerpt", "char_start": 0, "char_end": len(text), "excerpt": text}]
    content = F.blank_content(kind="measurement", source_wording=text, statement_type="exact", value_literal="18",
                              value_num=18.0, unit_original="mg/L", timepoint_literal="2 h", timepoint_value=2.0,
                              time_unit="h")
    spans = {"value_literal": [10, 12], "unit_original": [13, 17], "timepoint_literal": [21, 24]}
    assert F.grounding_problems(content, spans, {"supporting_excerpt": text}, locs) == []
    wrong = dict(spans, value_literal=[0, 2])
    problems = F.grounding_problems(content, wrong, {"supporting_excerpt": text}, locs)
    assert [p["field"] for p in problems] == ["value_literal"] and problems[0]["claimed_literal"] == "18"
    missing = {k: v for k, v in spans.items() if k != "unit_original"}
    assert [p["field"] for p in F.grounding_problems(content, missing, {"supporting_excerpt": text}, locs)] == [
        "unit_original"]
    # the excerpt itself must also be the stored text
    assert F.grounding_problems(content, spans, {"supporting_excerpt": "Something else entirely....."}, locs)
    assert F.grounding_problems(content, spans, {"supporting_excerpt": None}, locs)


# ------------------------------------------------------------------ extraction schema
def _valid(**over):
    return F.blank_content(**{"kind": "measurement", "source_wording": "x 5 mg", "statement_type": "exact",
                              "value_literal": "5", "value_num": 5.0, **over})


def test_schema_accepts_valid_content_and_rejects_unknown_fields():
    assert F.validate_content(_valid())["value_num"] == 5.0
    with pytest.raises(ValidationError):
        F.validate_content({**_valid(), "confidence_score": 0.9})


@pytest.mark.parametrize("over, message", [
    (dict(statement_type="range", value_low=1.0, value_high=2.0), "must not be stored as a point value"),
    (dict(statement_type="bound", bound_type="gt", value_low=5.0), "must not be stored as a point value"),
    (dict(statement_type="bound", bound_type="gt", value_num=None, value_high=5.0), "lower bound"),
    (dict(statement_type="qualitative"), "must not carry numeric values"),
    (dict(kind="narrative"), "must be 'qualitative' or 'unparsed'"),
    (dict(uncertainty_value=0.5), "need an uncertainty_type"),
    (dict(uncertainty_type="sd", uncertainty_value=0.5, confidence_level=95.0), "applies only to uncertainty_type"),
    (dict(timepoint_value=3.0), "needs a time_unit"),
    (dict(relative_semantics="percent_of_something"), "relative_semantics"),
    (dict(observation_type="guess"), "observation_type"),
    (dict(field_status={"uncertainty": "unknown"}), "field_status"),
    (dict(field_status={"not_a_field": "not_reported"}), "field_status key"),
    (dict(sample_size=-1), "sample_size"),
    (dict(source_wording=""), "source_wording"),
])
def test_schema_rejects_inconsistent_content(over, message):
    with pytest.raises(ValidationError, match=message):
        F.validate_content(_valid(**over))
