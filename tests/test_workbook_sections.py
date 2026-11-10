"""Stacked tables on one sheet (sections), column patterns, and numbers inside hyphenated names.

The workbook here is generated at test time. Its layout mirrors a common
hand-made evidence workbook: an observation table, a merged divider row, and a
second table of single measurements whose cells hold labelled lines.
"""

from __future__ import annotations

import openpyxl
import pytest

from conftest import OPERATOR
from geodesic import locations
from geodesic.errors import MappingError
from geodesic.synthetic.workbook import fixture_mapping
from geodesic.textparse import parse_text
from geodesic.util import canonical_json, loads, sha256_text
from geodesic.workbook.importer import import_workbook
from geodesic.workbook.mapping import WorkbookMapping, dump_mapping, validate_mapping

DOI_A = "https://doi.org/10.5555/syn.0001"
DOI_B = "https://doi.org/10.5555/syn.0002"


def _write_stacked_workbook(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Release"
    ws["A1"] = "Release: synthetic stacked layout"
    ws.merge_cells("A1:C1")
    ws.append(["Reference", "What was compared", "Observed"])
    ws.append([f"[Holding tank · dye] Alpha 2021 · Fig. 1\n{DOI_A}", "Tank A → tank B; same dye; 4 h.",
               "↑ dye signal in tank B at 4 h (12.5 ± 1.2 mg/L). Signal ≠ intact tracer."])
    ws.append([f"[Conduit · salt] Beta 2022 · Fig. 3\n{DOI_B}", "Pump off → pump on.",
               "TX-200 and Dye-5 raised conduit signal 3-fold."])
    ws["A5"] = "NUMERIC EVIDENCE"
    ws.merge_cells("A5:C5")
    ws.append([f"Q-001 · Alpha 2021 · Fig. 1\nSYN-1-003 · Holding tank\n{DOI_A}",
               "Dye concentration: 12.5 ± 1.2 mg/L\nTime / context: 4 h\nCondition: Tank B, pump on · Dose / route: 5 g",
               "Grab sample\nSingle sampling point; not flow-weighted."])
    ws.append([f"Q-002 · Beta 2022 · Fig. 3\nSYN-1-004 · Conduit\n{DOI_B}",
               "Conduit signal change: 3 fold\nTime / context: steady state\nCondition: Pump on",
               "Relative signal comparison\nApproximate ratio as reported."])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _mapping(**overrides) -> WorkbookMapping:
    observations = {
        "sheet": "Release", "role": "evidence", "confirmed": True, "header_row": 2, "data_start_row": 3,
        "data_end_row": 4, "merged_cells": "anchor_only",
        "stage": {"mode": "fixed", "stage_keys": ["release"], "confirmed": True},
        "columns": [
            {"column": "A", "role": "paper_reference", "confirmed": True,
             "patterns": [{"pattern": r"^\[(?P<value>[^\]]+)\]", "role": "system_context"}]},
            {"column": "B", "role": "experimental_context", "confirmed": True},
            {"column": "C", "role": "narrative_findings", "confirmed": True}]}
    numeric = {
        "sheet": "Release", "role": "evidence", "confirmed": True, "header_row": 5, "data_start_row": 6,
        "data_end_row": None, "merged_cells": "anchor_only",
        "stage": {"mode": "fixed", "stage_keys": ["release"], "confirmed": True},
        "columns": [
            {"column": "A", "role": "paper_reference", "confirmed": True,
             "patterns": [{"pattern": r"^[A-Z]+-\d+-\d+ · (?P<value>.+)$", "role": "system_context"}]},
            {"column": "B", "role": "measurements", "confirmed": True,
             "patterns": [{"pattern": r"\A(?P<value>[^:\n]+):", "role": "outcome"},
                          {"pattern": r"^Time / context:\s*(?P<value>.+)$", "role": "timepoint"},
                          {"pattern": r"^Condition:\s*(?P<value>.+)$", "role": "experimental_context"}]},
            {"column": "C", "role": "limitations", "confirmed": True,
             "patterns": [{"pattern": r"\A(?P<value>[^\n]+)", "role": "measurement_method"}]}]}
    observations.update(overrides.pop("observations", {}))
    numeric.update(overrides.pop("numeric", {}))
    return WorkbookMapping.model_validate({"workbook_key": "stacked", "sheets": [observations, numeric]})


@pytest.fixture
def stacked(project, tmp_path):
    path = _write_stacked_workbook(tmp_path / "inputs" / "stacked.xlsx")
    result = import_workbook(project, path, _mapping(), declare="synthetic", actor=OPERATOR)
    return project, result


def _finding(conn, fragment, kind=None):
    sql = "SELECT * FROM finding WHERE record_status = 'active' AND source_wording LIKE ?"
    params = [f"%{fragment}%"]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = conn.execute(sql, params).fetchall()
    assert len(rows) == 1, f"expected one finding with {fragment!r}, found {len(rows)}"
    return rows[0]


# ------------------------------------------------------------------ sections
def test_divider_row_between_sections_is_not_read_as_a_paper(stacked):
    project, result = stacked
    conn = project.conn
    citations = [r[0] for r in conn.execute("SELECT citation_text FROM paper")]
    assert len(citations) == 2
    assert not any("NUMERIC EVIDENCE" in (c or "") for c in citations)
    assert result.stats["evidence_rows_by_sheet"] == {"Release": 4}
    assert conn.execute("SELECT COUNT(*) FROM paper WHERE resolution_status = 'unresolved'").fetchone()[0] == 0


def test_each_section_uses_its_own_column_roles(stacked):
    conn = stacked[0].conn
    # observation section: column B is context, column C holds the findings
    obs = _finding(conn, "↑ dye signal in tank B")
    assert obs["experimental_context"] == "Tank A → tank B; same dye; 4 h."
    assert obs["system_context"] == "Holding tank · dye"
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE source_wording LIKE 'Tank A%'").fetchone()[0] == 0
    # numeric section: column B is the measurement, column C its limitation
    m = _finding(conn, "Dye concentration", kind="measurement")
    assert (m["value_literal"], m["unit_original"], m["uncertainty_literal"]) == ("12.5", "mg/L", "1.2")
    assert m["outcome"] == "Dye concentration"
    assert m["timepoint_literal"] == "4 h" and m["time_unit"] == "h"
    assert m["experimental_context"] == "Tank B, pump on · Dose / route: 5 g"
    assert m["measurement_method"] == "Grab sample"
    assert m["system_context"] == "Holding tank"
    assert "not flow-weighted" in m["interpretation_limits"]
    lim = _finding(conn, "Single sampling point", kind="limitation")
    assert lim["measurement_method"] == "Grab sample"


def test_lines_read_by_a_pattern_are_context_not_findings(stacked):
    conn = stacked[0].conn
    for text in ("Time / context", "Condition:", "Grab sample", "Relative signal comparison"):
        n = conn.execute("SELECT COUNT(*) FROM finding WHERE source_wording LIKE ?", (f"{text}%",)).fetchone()[0]
        assert n == 0, f"{text!r} was parsed as a finding"
    # the dose in the condition line is context, not a measurement
    assert conn.execute("SELECT COUNT(*) FROM finding WHERE kind = 'measurement' AND unit_original = 'g'"
                        ).fetchone()[0] == 0


def test_a_pattern_timepoint_that_is_not_a_time_is_kept_and_flagged(stacked):
    m = _finding(stacked[0].conn, "Conduit signal change", kind="measurement")
    assert m["timepoint_literal"] == "steady state"
    assert "timepoint_pattern_not_parsed" in loads(m["review_flags_json"], [])
    assert loads(m["field_status_json"], {})["timepoint"] == "could_not_be_parsed"


def test_pattern_fields_are_grounded_at_their_exact_characters(stacked):
    project, _ = stacked
    conn = project.conn
    m = _finding(conn, "Dye concentration", kind="measurement")
    assert m["grounding_status"] == "grounded"
    spans = loads(m["spans_json"], {})
    assert spans["timepoint_literal"]["role"] == "context:timepoint"
    locs = {l["role"]: l for l in locations.current_locations(conn, "finding", m["id"])}
    for role, value in (("context:outcome", "Dye concentration"), ("context:timepoint", "4 h"),
                        ("context:experimental_context", "Tank B, pump on · Dose / route: 5 g"),
                        ("context:measurement_method", "Grab sample"), ("context:system_context", "Holding tank")):
        assert locs[role]["excerpt"] == value
        ok, note, _ = locations.resolve(conn, locs[role])
        assert ok, f"{role}: {note}"
    for f in conn.execute("SELECT id, grounding_status FROM finding"):
        assert f["grounding_status"] == "grounded"


def test_reimporting_the_same_file_and_mapping_does_nothing(stacked, tmp_path):
    project, _ = stacked
    path = tmp_path / "inputs" / "stacked.xlsx"
    again = import_workbook(project, path, _mapping(), declare="synthetic", actor=OPERATOR)
    assert again.status == "already_imported"


# ------------------------------------------------------------------ validation
def _validate(mapping):
    validate_mapping(mapping, ["Release"], ["release", "transit", "retention", "discharge"])


def test_overlapping_sections_are_refused():
    with pytest.raises(MappingError, match="overlaps the section"):
        _validate(_mapping(observations={"data_end_row": 5}))


def test_upper_section_without_an_end_row_is_refused():
    with pytest.raises(MappingError, match="needs data_end_row"):
        _validate(_mapping(observations={"data_end_row": None}))


def test_a_sheet_listed_twice_other_than_as_evidence_sections_is_refused():
    with pytest.raises(MappingError, match="listed more than once"):
        _validate(_mapping(numeric={"role": "summary"}))


def test_pattern_needs_a_value_group_and_an_allowed_role():
    bad = _mapping().model_dump(mode="json")
    bad["sheets"][0]["columns"][0]["patterns"] = [{"pattern": r"^\[([^\]]+)\]", "role": "system_context"}]
    with pytest.raises(Exception, match="group named 'value'"):
        WorkbookMapping.model_validate(bad)
    bad["sheets"][0]["columns"][0]["patterns"] = [{"pattern": r"(?P<value>x)", "role": "narrative_findings"}]
    with pytest.raises(Exception, match="pattern role"):
        WorkbookMapping.model_validate(bad)


def test_pattern_role_may_not_repeat_a_column_role():
    m = _mapping().model_dump(mode="json")
    m["sheets"][0]["columns"][2]["patterns"] = [{"pattern": r"(?P<value>x)", "role": "experimental_context"}]
    with pytest.raises(MappingError, match="both a column role and a pattern role"):
        _validate(WorkbookMapping.model_validate(m))


def test_mapping_without_patterns_keeps_its_content_hash():
    m = fixture_mapping(confirmed=True)
    legacy = m.model_dump(mode="json")
    for sheet in legacy["sheets"]:
        for col in sheet["columns"]:
            del col["patterns"]
    assert m.content_sha256() == sha256_text(canonical_json(legacy))
    assert "patterns" not in dump_mapping(m).split("\n\n", 1)[1]


# ------------------------------------------------------------------ hyphenated names
def test_numbers_inside_hyphenated_names_are_not_quantities():
    p = parse_text("TX-200 and Dye-5 raised the signal 3-fold; batch LOT-1-005 at 4 h.")
    literals = [q.value_literal for q in p.quantities]
    assert "200" not in literals and "5" not in literals and "005" not in literals and "1" not in literals
    assert "3" in literals
    assert {u["text"] for u in p.unparsed if u["reason"] == "number_in_hyphenated_name"} == {"200", "5", "1", "005"}


def test_ranges_and_signed_numbers_are_unaffected_by_the_name_rule():
    p = parse_text("values ranged 10-20 mg/L; a change of -5 °C; pH 7.4-7.6")
    assert [q.value_literal for q in p.quantities] == ["10-20", "-5", "7.4-7.6"]


def test_names_in_imported_text_do_not_become_measurements(stacked):
    conn = stacked[0].conn
    f = conn.execute("SELECT value_literal FROM finding WHERE source_wording LIKE 'TX-200%' AND kind='measurement'"
                     ).fetchall()
    assert [r[0] for r in f] == ["3"]
