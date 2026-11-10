"""Read an .xlsx workbook into plain cell records without modifying it.

The workbook is opened read-only from disk twice: once for formulas and
formatting, once for cached formula results. Every non-empty cell (value,
comment or hyperlink) becomes a :class:`CellRecord` that preserves the cell
text, coordinates, hyperlink, comment, merge membership and basic formatting.

Conventions: rows and columns are 1-based, as in the spreadsheet (``A1`` is
row 1, column 1). Character spans into ``raw_text`` are 0-based, half-open.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries

from ..errors import GeodesicError

READER_NAME = "openpyxl"


@dataclass
class CellRecord:
    sheet: str
    cell_ref: str
    row: int
    col: int
    raw_text: str | None
    value_type: str                 # str | number | bool | date | formula | empty
    formula: str | None = None
    hyperlink: str | None = None
    comment: str | None = None
    comment_author: str | None = None
    merged_range: str | None = None
    is_merge_anchor: bool = False
    fill_color: str | None = None
    font_flags: str | None = None
    number_format: str | None = None


@dataclass
class SheetData:
    name: str
    max_row: int
    max_col: int
    cells: dict[tuple[int, int], CellRecord] = field(default_factory=dict)
    merged_ranges: list[str] = field(default_factory=list)
    #: (row, col) of every cell inside a merged range -> (anchor_row, anchor_col, range_ref)
    merge_anchor: dict[tuple[int, int], tuple[int, int, str]] = field(default_factory=dict)
    hidden: bool = False

    def cell(self, row: int, col: int) -> CellRecord | None:
        return self.cells.get((row, col))

    def effective(self, row: int, col: int, fill_down: bool = True) -> CellRecord | None:
        """The cell holding the text that applies at (row, col).

        Inside a merged range this is the anchor (top-left) cell, because that is
        where the spreadsheet stores the text. With ``fill_down=False`` only the
        anchor position itself resolves.
        """
        rec = self.cells.get((row, col))
        if rec is not None and rec.raw_text not in (None, ""):
            return rec
        if fill_down and (row, col) in self.merge_anchor:
            ar, ac, _ = self.merge_anchor[(row, col)]
            return self.cells.get((ar, ac))
        return rec


@dataclass
class WorkbookData:
    path: Path
    sheets: list[SheetData]

    def sheet(self, name: str) -> SheetData | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None

    @property
    def n_cells(self) -> int:
        return sum(len(s.cells) for s in self.sheets)


def cell_text(value) -> tuple[str | None, str]:
    """Stable text form and type of a cell value."""
    if value is None:
        return None, "empty"
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), "bool"
    if isinstance(value, int):
        return str(value), "number"
    if isinstance(value, float):
        return (str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)), "number"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat(), "date"
    if isinstance(value, _dt.timedelta):
        return str(value), "date"
    return str(value), "str"


def _color(cell) -> str | None:
    fill = cell.fill
    if fill is None or fill.fill_type in (None, "none"):
        return None
    color = fill.fgColor
    if color is None:
        return None
    if color.type == "rgb" and color.rgb and isinstance(color.rgb, str):
        return None if color.rgb in ("00000000",) else color.rgb
    if color.type == "theme":
        return f"theme:{color.theme}"
    if color.type == "indexed":
        return f"indexed:{color.indexed}"
    return None


def _font_flags(cell) -> str | None:
    font = cell.font
    if font is None:
        return None
    flags = []
    if font.b:
        flags.append("bold")
    if font.i:
        flags.append("italic")
    if font.strike:
        flags.append("strike")
    if font.underline:
        flags.append("underline")
    color = font.color
    if color is not None and color.type == "rgb" and isinstance(color.rgb, str) and color.rgb not in (
            "FF000000", "00000000"):
        flags.append(f"color:{color.rgb}")
    return ",".join(flags) or None


def read_workbook(path: Path | str) -> WorkbookData:
    """Read every worksheet. The file is never written to."""
    path = Path(path)
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise GeodesicError(
            f"Unsupported workbook format {path.suffix!r}. Supported: .xlsx and .xlsm (macros are never executed). "
            "Save legacy .xls files as .xlsx first.")
    try:
        wb_f = load_workbook(path, data_only=False, read_only=False, keep_vba=False)
        wb_v = load_workbook(path, data_only=True, read_only=False, keep_vba=False)
    except Exception as exc:  # openpyxl raises a variety of errors for damaged files
        raise GeodesicError(f"Could not read workbook {path}: {exc}") from exc
    sheets: list[SheetData] = []
    try:
        for ws in wb_f.worksheets:
            wv = wb_v[ws.title]
            sd = SheetData(name=ws.title, max_row=ws.max_row or 0, max_col=ws.max_column or 0,
                           hidden=ws.sheet_state != "visible")
            for rng in ws.merged_cells.ranges:
                ref = rng.coord
                sd.merged_ranges.append(ref)
                c1, r1, c2, r2 = range_boundaries(ref)
                for r in range(r1, r2 + 1):
                    for c in range(c1, c2 + 1):
                        sd.merge_anchor[(r, c)] = (r1, c1, ref)
            for row in ws.iter_rows():
                for cell in row:
                    value = cell.value
                    has_comment = cell.comment is not None
                    has_link = cell.hyperlink is not None
                    if value is None and not has_comment and not has_link:
                        continue
                    r, c = cell.row, cell.column
                    formula = None
                    if cell.data_type == "f" or (isinstance(value, str) and value.startswith("=")):
                        formula = str(value)
                        text, _ = cell_text(wv.cell(row=r, column=c).value)
                        vtype = "formula"
                    else:
                        text, vtype = cell_text(value)
                    link = None
                    if has_link:
                        link = cell.hyperlink.target or (
                            f"#{cell.hyperlink.location}" if cell.hyperlink.location else None)
                    merged = sd.merge_anchor.get((r, c))
                    sd.cells[(r, c)] = CellRecord(
                        sheet=ws.title, cell_ref=f"{get_column_letter(c)}{r}", row=r, col=c, raw_text=text,
                        value_type=vtype, formula=formula, hyperlink=link,
                        comment=cell.comment.text if has_comment else None,
                        comment_author=cell.comment.author if has_comment else None,
                        merged_range=merged[2] if merged else None,
                        is_merge_anchor=bool(merged and merged[0] == r and merged[1] == c),
                        fill_color=_color(cell), font_flags=_font_flags(cell),
                        number_format=cell.number_format if cell.number_format != "General" else None,
                    )
            sheets.append(sd)
    finally:
        wb_f.close()
        wb_v.close()
    return WorkbookData(path=path, sheets=sheets)
