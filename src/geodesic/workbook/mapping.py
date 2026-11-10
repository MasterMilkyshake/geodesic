"""The human-editable workbook mapping file.

A mapping tells the importer which sheets and columns hold paper references,
stage assignments, narrative findings, measurements and limitations. A new
workbook layout means editing a mapping file, not code.

Every entry that the profiler *inferred* is written with ``confirmed: false``.
The importer refuses to run until a person has checked each required entry and
set it to ``confirmed: true``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from openpyxl.utils import column_index_from_string
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import MappingError, UnconfirmedMappingError
from ..util import canonical_json, sha256_text

MAPPING_VERSION = 1

#: Column roles. ``unmapped`` columns are still stored cell-by-cell and retained per row as unknown fields.
COLUMN_ROLES = (
    "paper_reference", "doi", "pmid", "url", "title", "authors", "year", "journal",
    "stage", "narrative_findings", "measurements", "limitations",
    "system_context", "experimental_context", "intervention", "comparator", "outcome",
    "measurement_method", "timepoint", "unmapped",
)
PAPER_IDENTITY_ROLES = ("paper_reference", "doi", "pmid", "title")
CONTENT_ROLES = ("narrative_findings", "measurements", "limitations")
CONTEXT_ROLES = ("system_context", "experimental_context", "intervention", "comparator", "outcome",
                 "measurement_method")
SINGLE_ROLES = ("paper_reference", "doi", "pmid", "url", "title", "authors", "year", "journal", "stage",
                *CONTEXT_ROLES, "timepoint")
SUMMARY_MEASURES = ("evidence_rows", "distinct_papers")
#: Fields a column pattern may fill. Each becomes row context for the findings of the row.
PATTERN_ROLES = (*CONTEXT_ROLES, "timepoint")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatternField(_Model):
    """A field read from part of a cell's text by a regular expression.

    The pattern is applied with ``re.MULTILINE`` (``^`` and ``$`` match at line
    breaks) and must define a group named ``value``. The first match in the
    cell gives the field, with the exact character span of the value. A
    content segment that lies wholly inside a match is context and is not
    parsed into findings.
    """

    pattern: str
    role: str

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in PATTERN_ROLES:
            raise ValueError(f"pattern role {v!r} is not allowed; allowed: {', '.join(PATTERN_ROLES)}")
        return v

    @field_validator("pattern")
    @classmethod
    def _pattern(cls, v: str) -> str:
        try:
            rx = re.compile(v, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"pattern {v!r} is not a valid regular expression: {exc}") from exc
        if "value" not in rx.groupindex:
            raise ValueError(f"pattern {v!r} has no group named 'value' (write it as (?P<value>...))")
        return v

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.MULTILINE)


class ColumnMapping(_Model):
    column: str                      # spreadsheet column letter, e.g. "C"
    header: str | None = None        # header text seen by the profiler (documentation only)
    role: str = "unmapped"
    confirmed: bool = False
    inferred_by: str | None = None   # why the profiler proposed this role
    note: str | None = None
    patterns: list[PatternField] = Field(default_factory=list)   # fields read from part of the cell text

    @field_validator("column")
    @classmethod
    def _col(cls, v: str) -> str:
        v = v.strip().upper()
        try:
            column_index_from_string(v)
        except Exception as exc:
            raise ValueError(f"{v!r} is not a spreadsheet column letter") from exc
        return v

    @field_validator("role")
    @classmethod
    def _role(cls, v: str) -> str:
        if v not in COLUMN_ROLES:
            raise ValueError(f"unknown column role {v!r}; allowed: {', '.join(COLUMN_ROLES)}")
        return v

    @property
    def index(self) -> int:
        return column_index_from_string(self.column)


class StageMapping(_Model):
    """How rows of a sheet are assigned to project stages.

    ``fixed``: every row of the sheet belongs to ``stage_keys``.
    ``column``: the column with role ``stage`` names the stage(s); ``value_map``
    translates the workbook's own terms to project stage keys and ``separator``
    splits cells naming several stages.
    ``none``: the sheet carries no stage assignment.
    """

    mode: Literal["fixed", "column", "none"] = "none"
    stage_keys: list[str] = Field(default_factory=list)
    value_map: dict[str, str] = Field(default_factory=dict)
    separator: str = ";"
    confirmed: bool = False
    inferred_by: str | None = None


class SheetMapping(_Model):
    sheet: str
    role: Literal["evidence", "summary", "ignore"] = "ignore"
    confirmed: bool = False
    inferred_by: str | None = None
    header_row: int | None = None        # 1-based
    data_start_row: int | None = None    # 1-based
    data_end_row: int | None = None      # 1-based inclusive; null = last used row
    merged_cells: Literal["fill_down", "anchor_only"] = "fill_down"
    stage: StageMapping = Field(default_factory=StageMapping)
    columns: list[ColumnMapping] = Field(default_factory=list)

    def columns_with(self, *roles: str) -> list[ColumnMapping]:
        return [c for c in self.columns if c.role in roles]

    def column_for(self, role: str) -> ColumnMapping | None:
        cols = self.columns_with(role)
        return cols[0] if cols else None


class SummaryCount(_Model):
    """A cell on a summary sheet holding a count the importer must reconcile."""

    sheet: str
    cell: str
    measures: Literal["evidence_rows", "distinct_papers"]
    stage_key: str | None = None     # null = whole workbook
    label: str | None = None
    confirmed: bool = False


class WorkbookMapping(_Model):
    mapping_version: int = MAPPING_VERSION
    workbook_key: str = "main"       # names the workbook lineage; later versions of the same workbook reuse it
    generated_from: dict = Field(default_factory=dict)
    sheets: list[SheetMapping] = Field(default_factory=list)
    summary_counts: list[SummaryCount] = Field(default_factory=list)

    def sheet(self, name: str) -> SheetMapping | None:
        for s in self.sheets:
            if s.sheet == name:
                return s
        return None

    def sections(self, name: str) -> list[SheetMapping]:
        """Every entry for sheet ``name`` (a sheet may hold several evidence sections), top to bottom."""
        return sorted((s for s in self.sheets if s.sheet == name), key=lambda s: s.data_start_row or 0)

    def plain(self) -> dict[str, Any]:
        """The mapping as plain data, without empty ``patterns`` lists.

        Leaving the empty lists out keeps the content hash of a mapping written
        before column patterns existed unchanged.
        """
        data = self.model_dump(mode="json")
        for sheet in data["sheets"]:
            for col in sheet["columns"]:
                if not col.get("patterns"):
                    col.pop("patterns", None)
        return data

    def content_sha256(self) -> str:
        return sha256_text(canonical_json(self.plain()))


def load_mapping(path: Path | str) -> WorkbookMapping:
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise MappingError(f"Mapping file not found: {p}") from exc
    except yaml.YAMLError as exc:
        raise MappingError(f"Mapping file {p} is not valid YAML: {exc}") from exc
    try:
        return WorkbookMapping.model_validate(data)
    except Exception as exc:
        raise MappingError(f"Invalid mapping file {p}:\n{exc}") from exc


_HEADER = """\
# GEODESIC workbook mapping
#
# This file tells the importer where things are in your workbook. Entries marked
# `confirmed: false` were INFERRED by the profiler from header text only. They
# are guesses. Check each one against the workbook, correct it, then set
# `confirmed: true`. The importer refuses to run while any required entry is
# unconfirmed.
#
# Sheet roles:   evidence | summary | ignore
# Column roles:  {roles}
#   paper_reference     citation text for the paper the row is about
#   narrative_findings  free text that may hold several facts per cell
#   measurements        cells that each hold one measurement (value and unit)
#   limitations         caveats and limits on interpretation
#   unmapped            kept cell-by-cell and as an unknown field of the row; not interpreted
# Column patterns (optional): fields read from part of a cell's text, e.g.
#     patterns:
#       - pattern: '^Condition:\\s*(?P<value>.+)$'    # regular expression, multiline, group 'value'
#         role: experimental_context                  # {pattern_roles}
#   A content line wholly inside a match is context, not a finding.
# Sections: a sheet holding several stacked tables may be listed once per table,
#   each entry an evidence section with its own header_row, data rows (data_end_row
#   required on all but the lowest), columns and stage. Row ranges may not overlap.
# Stage modes:   fixed (whole sheet = stage_keys) | column (a `stage` column names it) | none
# summary_counts: cells holding counts that the importer reconciles against what it imported
#   measures: evidence_rows | distinct_papers ; stage_key: a project stage key, or null for the whole workbook
#   example:
#     - sheet: Summary
#       cell: B2
#       measures: evidence_rows
#       stage_key: <stage key>
#       confirmed: true
"""


def dump_mapping(mapping: WorkbookMapping) -> str:
    body = yaml.safe_dump(mapping.plain(), sort_keys=False, allow_unicode=True, width=110)
    return _HEADER.format(roles=" | ".join(COLUMN_ROLES), pattern_roles=" | ".join(PATTERN_ROLES)) + "\n" + body


def write_mapping(mapping: WorkbookMapping, path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(dump_mapping(mapping), encoding="utf-8")


def _section_problems(name: str, sections: list[SheetMapping]) -> list[str]:
    """Problems with a sheet listed more than once: only as evidence sections with separate row ranges."""
    if any(s.role != "evidence" for s in sections):
        return [f"sheet {name!r} is listed more than once; a sheet may be listed more than once only as "
                "evidence sections with separate row ranges"]
    if any(s.header_row is None or s.data_start_row is None for s in sections):
        return []     # reported for each section on its own
    problems: list[str] = []
    for upper, lower in zip(sections, sections[1:]):
        if upper.data_end_row is None:
            problems.append(f"sheet {name!r}: the section from row {upper.data_start_row} needs data_end_row, "
                            f"because another section starts below it (row {lower.data_start_row})")
        elif upper.data_end_row >= min(lower.header_row, lower.data_start_row):
            problems.append(f"sheet {name!r}: the section with rows {upper.data_start_row}-{upper.data_end_row} "
                            f"overlaps the section with header row {lower.header_row}")
    return problems


def validate_mapping(mapping: WorkbookMapping, sheet_names: list[str], stage_keys: list[str]) -> None:
    """Check a mapping against the workbook and project. Raises with every problem listed.

    Structural problems raise :class:`MappingError`; if the structure is sound
    but required entries are unconfirmed, :class:`UnconfirmedMappingError`.
    """
    problems: list[str] = []
    unconfirmed: list[str] = []
    if mapping.mapping_version != MAPPING_VERSION:
        problems.append(f"mapping_version {mapping.mapping_version} is not supported (expected {MAPPING_VERSION})")
    mapped = [s.sheet for s in mapping.sheets]
    for name in sorted(set(mapped)):
        if mapped.count(name) > 1:
            problems.extend(_section_problems(name, mapping.sections(name)))
    for name in sheet_names:
        if name not in mapped:
            problems.append(f"workbook sheet {name!r} is not listed in the mapping (list it with role: ignore "
                            "if it holds no evidence)")
    for name in mapped:
        if name not in sheet_names:
            problems.append(f"mapping lists sheet {name!r}, which is not in the workbook")
    if not any(s.role == "evidence" for s in mapping.sheets):
        problems.append("no sheet has role 'evidence'")
    for sm in mapping.sheets:
        where = f"sheet {sm.sheet!r}"
        if mapped.count(sm.sheet) > 1:
            where += f" (section from row {sm.data_start_row})"
        if not sm.confirmed:
            unconfirmed.append(f"{where}: role '{sm.role}' (and its header/data rows)")
        if sm.role != "evidence":
            continue
        if sm.header_row is None or sm.data_start_row is None:
            problems.append(f"{where}: header_row and data_start_row are required for evidence sheets")
        elif sm.data_start_row <= sm.header_row:
            problems.append(f"{where}: data_start_row must be below header_row")
        letters = [c.column for c in sm.columns]
        for letter in sorted(set(letters)):
            if letters.count(letter) > 1:
                problems.append(f"{where}: column {letter} is listed more than once")
        for role in SINGLE_ROLES:
            if len(sm.columns_with(role)) > 1:
                problems.append(f"{where}: more than one column has role '{role}'")
        if not sm.columns_with(*PAPER_IDENTITY_ROLES):
            problems.append(f"{where}: no column identifies the paper (one of: {', '.join(PAPER_IDENTITY_ROLES)})")
        for cm in sm.columns:
            if (cm.role != "unmapped" or cm.patterns) and not cm.confirmed:
                unconfirmed.append(f"{where}: column {cm.column} ({cm.header or 'no header'}) as '{cm.role}'"
                                   + (f" with {len(cm.patterns)} pattern(s)" if cm.patterns else ""))
        column_roles = {c.role for c in sm.columns}
        pattern_roles = [pf.role for c in sm.columns for pf in c.patterns]
        for role in sorted(set(pattern_roles)):
            if pattern_roles.count(role) > 1:
                problems.append(f"{where}: more than one pattern fills '{role}'")
            if role in column_roles:
                problems.append(f"{where}: '{role}' is both a column role and a pattern role")
        st = sm.stage
        if not st.confirmed:
            unconfirmed.append(f"{where}: stage assignment (mode '{st.mode}')")
        if st.mode == "fixed":
            if not st.stage_keys:
                problems.append(f"{where}: stage mode 'fixed' needs at least one entry in stage_keys")
            for key in st.stage_keys:
                if key not in stage_keys:
                    problems.append(f"{where}: stage key {key!r} is not a stage of this project "
                                    f"(project stages: {', '.join(stage_keys) or 'none'})")
        elif st.mode == "column":
            if sm.column_for("stage") is None:
                problems.append(f"{where}: stage mode 'column' needs a column with role 'stage'")
            for term, key in st.value_map.items():
                if not key:
                    unconfirmed.append(f"{where}: stage term {term!r} has no stage key in value_map (enter a "
                                       "project stage key, or delete the line to keep the term as unmapped content)")
                elif key not in stage_keys:
                    problems.append(f"{where}: value_map sends {term!r} to unknown stage key {key!r}")
        elif sm.column_for("stage") is not None:
            problems.append(f"{where}: a column has role 'stage' but stage mode is 'none'")
    for sc in mapping.summary_counts:
        where = f"summary count {sc.sheet}!{sc.cell}"
        if sc.sheet not in sheet_names:
            problems.append(f"{where}: sheet is not in the workbook")
        if sc.stage_key is not None and sc.stage_key not in stage_keys:
            problems.append(f"{where}: unknown stage key {sc.stage_key!r}")
        if not sc.confirmed:
            unconfirmed.append(f"{where}: measures '{sc.measures}'")
    if problems:
        raise MappingError("The mapping file has problems:\n  - " + "\n  - ".join(problems))
    if unconfirmed:
        raise UnconfirmedMappingError(
            f"The importer will not run: {len(unconfirmed)} required mapping entr"
            f"{'y is' if len(unconfirmed) == 1 else 'ies are'} unconfirmed. Check each against the workbook, "
            "correct it if needed, and set `confirmed: true`:\n  - " + "\n  - ".join(unconfirmed))
