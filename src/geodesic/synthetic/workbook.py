"""Synthetic fixture workbook: an inert tracer moving among engineered reservoirs.

Everything here is invented for testing. The papers, authors, journals, DOIs
(``10.5555/...`` is a prefix reserved for examples) and values do not exist.
The workbook carries the embedded GEODESIC synthetic tag, so it can only be
imported into a synthetic project.

The fixture exercises: one sheet per stage; narrative cells holding several
facts; qualifiers, ranges, bounds and an unspecified ``±``; merged cells,
hyperlinks and comments; fill colour; repeated findings; a paper in two stages;
an unresolved citation; a malformed DOI; conflicting DOIs; a row without a
paper reference; and summary counts, two of which are deliberately wrong so the
importer has something to reconcile.

Version 2 moves rows, adds a row, removes a row and changes two findings.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.packaging.custom import StringProperty
from openpyxl.styles import Font, PatternFill

from ..config import ConceptGroup, ProjectConfig, SearchConfig, StageConfig
from ..provenance import SYNTHETIC_LABEL, TAG_PROPERTY
from ..workbook.mapping import ColumnMapping, SheetMapping, StageMapping, SummaryCount, WorkbookMapping

STAGES = (
    ("release", "Release", "Tracer leaving the source tank."),
    ("transit", "Transit", "Tracer moving through conduits between reservoirs."),
    ("retention", "Retention", "Tracer held in holding reservoirs or on surfaces."),
    ("discharge", "Discharge", "Tracer leaving the system through the outflow."),
)
HEADERS = ("Reference", "DOI", "System", "Key findings", "Measured value", "Limitations", "Reviewer notes", "Year")
HEADER_ROW = 3

#: key -> (citation text, DOI column text, hyperlink on the reference cell, year)
PAPERS = {
    "P1": ("Arden Q, Belmont T. Tracer release from a stirred source tank. J Synth Hydraul. 2019;4:11-20.",
           "10.5555/geodesic.synth.001", None, 2019),
    "P2": ("Castellan R, Dunmore L, Eastlake P. Liner binding of an inert tracer. Synth Reserv Eng. 2020;7:101-109. "
           "doi:10.5555/geodesic.synth.002", None, None, 2020),
    "P3": ("Corwin & Dale, internal test report, undated", None, None, None),
    "P4": ("Fenwick A, Garrow H. Conduit transit of a dye pulse. J Synth Hydraul. 2021;6:55-63.",
           None, "https://doi.org/10.5555/geodesic.synth.004", 2021),
    "P5": ("Hallam J. Minimum flow for complete mixing. Proc Synth Flow Conf. 2018.", "10.555/bad", None, 2018),
    "P6": ("Ingleby K, Jarrow M. Holding reservoir retention. Synth Reserv Eng. 2022;9:12-25. "
           "doi:10.5555/geodesic.synth.006", "10.5555/geodesic.synth.060", None, 2022),
    "P7": ("Kestrel N. Bypass line behaviour. J Synth Hydraul. 2017;2:1-9.",
           "https://doi.org/10.5555/GEODESIC.SYNTH.007", None, 2017),
    "P8": ("Lorimer S, Marlow D. Discharge clearance of an inert tracer. Synth Reserv Eng. 2023;10:200-214.",
           "doi:10.5555/geodesic.synth.008", None, 2023),
}


def _rows(version: int) -> dict[str, list[dict]]:
    """Row specifications per stage sheet. ``merge`` = number of rows the reference/DOI cells span."""
    release = [
        dict(ref="P1", merge=2, system="Stirred source tank, 200 L",
             findings="Tracer release was approximately 42 % of the loaded mass at 24 h (n = 6 runs); denominator "
                      "unclear. Release rate stayed > 5 mL/min throughout.",
             value="12.5 ± 1.3 mg/L (SD)", limits="Single site; not independently verified.",
             notes="check panel B", comment="Value read from the summary table, not the text."),
        dict(ref=None, system="Stirred source tank, 200 L", findings="Release half-time was 4.5 h ± 0.5."),
        dict(ref="P2", system="Lined source tank",
             findings="Between 10 and 20 % of the tracer remained bound to the liner after 48 h. Release was "
                      "complete by day 7.",
             value="0.8–1.2 1/h", limits="Liner batch not reported.", fill="FFFF00"),
        dict(ref="P3", system="Pilot tank", findings="Release appeared faster at higher stirring speed."),
        dict(ref="", system="", findings="Orphan note: 3 mg/L seen once."),
    ]
    transit = [
        dict(ref="P1", system="Main conduit",
             findings="Tracer release was approximately 42 % of the loaded mass at 24 h (n = 6 runs)."),
        dict(ref="P4", system="Main conduit",
             findings="Transit signal through the conduit was 340 a.u. (95% CI 300-380), mean of 4 runs. Peak "
                      "arrival at 2 h.",
             value="2.5-fold higher than baseline at day 7"),
        dict(ref="P5", system="Mixing loop",
             findings="Flow of at least 30 L/min was required; below this, mixing was incomplete.",
             value="1,5 mg"),
        dict(ref="P2", system="Return conduit", findings="Dispersion increased from 2 to 8 mL/min with flow rate."),
    ]
    retention = [
        dict(ref="P6", system="Holding reservoir A",
             findings="Retention in the holding reservoir was 65 % of inflow per litre at 12 h; 30 % of counted "
                      "particles were retained.",
             value="< 0.05 mg/kg", limits="Detection limit not stated; units unclear.", fill="FFC7CE"),
        dict(ref="P7", merge=2, system="Bypass line", findings="Retained fraction ~ 15 % (n = 3)."),
        dict(ref=None, system="Bypass line", findings="No retention was observed in the bypass line."),
    ]
    discharge = [
        dict(ref="P8", system="Outflow weir",
             findings="Discharge cleared 90 % of the tracer within 72 h. Clearance rate 0.12 ± 0.02 1/h (SE), "
                      "n = 5 reservoirs.",
             value="18 mg/L"),
        dict(ref="P2", system="Outflow weir", findings="Residual concentration ≤ 0.5 mg/L after 10 days."),
        dict(ref="P3", system="Outflow weir",
             findings="Discharge data were read from graph; values approximately 7 to 9 L/min."),
    ]
    if version == 2:
        # rows moved: a new first row shifts everything down, and P2/P3 swap places
        release = [dict(ref="P8", system="Secondary tank",
                        findings="Release from the secondary tank reached 55 % of the loaded mass at 24 h."),
                   release[0], release[1], release[3], release[2], release[4]]
        transit = transit[:3]                                           # one row removed
        retention[2] = dict(retention[2], findings="No retention was observed in the bypass line during the test.")
        discharge[0] = dict(discharge[0], value="21 mg/L")              # a value changed
    return {"Release": release, "Transit": transit, "Retention": retention, "Discharge": discharge}


def true_counts(version: int = 1) -> dict[str, dict[str, int]]:
    """Evidence rows and distinct papers per sheet as the fixture actually contains them."""
    out: dict[str, dict[str, int]] = {}
    everyone: set[str] = set()
    total = 0
    for sheet, rows in _rows(version).items():
        current, n, seen = None, 0, set()
        for r in rows:
            if r["ref"]:
                current = r["ref"]
            elif r["ref"] == "":
                current = None
            if current:
                n += 1
                seen.add(current)
        out[sheet] = {"evidence_rows": n, "distinct_papers": len(seen)}
        everyone |= seen
        total += n
    out["__all__"] = {"evidence_rows": total, "distinct_papers": len(everyone)}
    return out


def write_fixture_workbook(path: Path | str, version: int = 1) -> Path:
    """Write the synthetic fixture workbook (version 1 or 2) and return its path."""
    if version not in (1, 2):
        raise ValueError("fixture workbook version must be 1 or 2")
    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    readme["A1"] = SYNTHETIC_LABEL
    readme["A2"] = ("Invented test fixture: an inert tracer moving among engineered reservoirs. No paper, author, "
                    "journal, identifier or value in this workbook exists.")
    readme["A1"].font = Font(bold=True)
    bold = Font(bold=True)
    for sheet, rows in _rows(version).items():
        ws = wb.create_sheet(sheet)
        ws["A1"] = f"{sheet} evidence (synthetic)"
        ws["A1"].font = bold
        for i, h in enumerate(HEADERS, start=1):
            c = ws.cell(row=HEADER_ROW, column=i, value=h)
            c.font = bold
            c.fill = PatternFill("solid", fgColor="D9D9D9")
        r = HEADER_ROW + 1
        for spec in rows:
            if spec["ref"]:
                citation, doi, link, year = PAPERS[spec["ref"]]
                ws.cell(row=r, column=1, value=citation)
                if link:
                    ws.cell(row=r, column=1).hyperlink = link
                if doi:
                    ws.cell(row=r, column=2, value=doi)
                if year:
                    ws.cell(row=r, column=8, value=year)
                span = spec.get("merge", 1)
                if span > 1:
                    ws.merge_cells(start_row=r, start_column=1, end_row=r + span - 1, end_column=1)
                    ws.merge_cells(start_row=r, start_column=2, end_row=r + span - 1, end_column=2)
            for col, key in ((3, "system"), (4, "findings"), (5, "value"), (6, "limits"), (7, "notes")):
                if spec.get(key):
                    ws.cell(row=r, column=col, value=spec[key])
            if spec.get("comment"):
                ws.cell(row=r, column=4).comment = Comment(spec["comment"], "Synthetic Reviewer")
            if spec.get("fill"):
                ws.cell(row=r, column=5).fill = PatternFill("solid", fgColor=spec["fill"])
            r += 1
    counts = true_counts(version)
    ws = wb.create_sheet("Summary")
    for i, h in enumerate(("Stage", "Rows", "Papers"), start=1):
        ws.cell(row=1, column=i, value=h).font = bold
    for i, (_, name, _) in enumerate(STAGES, start=2):
        ws.cell(row=i, column=1, value=name)
        ws.cell(row=i, column=2, value=counts[name]["evidence_rows"])
        ws.cell(row=i, column=3, value=counts[name]["distinct_papers"])
    ws["A6"] = "Total"
    ws["B6"] = counts["__all__"]["evidence_rows"]
    ws["C6"] = counts["__all__"]["distinct_papers"]
    ws["A8"] = "Check"
    ws["B8"] = "=SUM(B2:B5)"
    if version == 1:
        # deliberate errors for the importer to report (the true values are 3 and 14)
        ws["B4"] = counts["Retention"]["evidence_rows"] + 1
        ws["B6"] = counts["__all__"]["evidence_rows"] + 1
    wb.custom_doc_props.append(StringProperty(name=TAG_PROPERTY, value="synthetic"))
    fixed = dt.datetime(2000, 1, 1)
    wb.properties.created = fixed
    wb.properties.modified = fixed
    wb.properties.creator = "GEODESIC synthetic fixture generator"
    wb.properties.title = SYNTHETIC_LABEL
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def fixture_mapping(confirmed: bool = True) -> WorkbookMapping:
    """The mapping for the fixture workbook. ``confirmed=False`` gives the same mapping with nothing confirmed."""
    roles = ("paper_reference", "doi", "system_context", "narrative_findings", "measurements", "limitations",
             "unmapped", "year")
    sheets = [SheetMapping(sheet="README", role="ignore", confirmed=confirmed)]
    for key, name, _ in STAGES:
        sheets.append(SheetMapping(
            sheet=name, role="evidence", confirmed=confirmed, header_row=HEADER_ROW, data_start_row=HEADER_ROW + 1,
            stage=StageMapping(mode="fixed", stage_keys=[key], confirmed=confirmed),
            columns=[ColumnMapping(column="ABCDEFGH"[i], header=HEADERS[i], role=roles[i], confirmed=confirmed)
                     for i in range(len(HEADERS))]))
    sheets.append(SheetMapping(sheet="Summary", role="summary", confirmed=confirmed))
    summary = []
    for i, (key, name, _) in enumerate(STAGES, start=2):
        summary.append(SummaryCount(sheet="Summary", cell=f"B{i}", measures="evidence_rows", stage_key=key,
                                    label=f"{name} rows", confirmed=confirmed))
        summary.append(SummaryCount(sheet="Summary", cell=f"C{i}", measures="distinct_papers", stage_key=key,
                                    label=f"{name} papers", confirmed=confirmed))
    summary.append(SummaryCount(sheet="Summary", cell="B6", measures="evidence_rows", label="Total rows",
                                confirmed=confirmed))
    summary.append(SummaryCount(sheet="Summary", cell="C6", measures="distinct_papers", label="Total papers",
                                confirmed=confirmed))
    return WorkbookMapping(workbook_key="main", sheets=sheets, summary_counts=summary,
                           generated_from={"note": "bundled mapping for the synthetic fixture workbook"})


def example_config(name: str = "Synthetic tracer example") -> ProjectConfig:
    """Project configuration for the synthetic example (four invented stages)."""
    return ProjectConfig(
        name=name,
        stages=[StageConfig(key=k, name=n, definition=d) for k, n, d in STAGES],
        reviewers=["Synthetic Reviewer A", "Synthetic Reviewer B"],
        search=SearchConfig(concepts=[
            ConceptGroup(label="tracer", terms=["inert tracer", "dye tracer", "tracer"]),
            ConceptGroup(label="system", terms=["reservoir", "tank", "conduit"])]),
    )
