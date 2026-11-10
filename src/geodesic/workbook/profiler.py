"""Workbook profiler and starter-mapping generator.

Run locally on a workbook to learn its layout before importing anything:
sheets, likely header rows, a column inventory, row counts, merged cells,
hyperlinks, cell comments and formatting that may carry meaning. Formatting is
reported as found; the profiler never guesses what a colour or font means.

The profile describes structure only. Apart from header text and (for a stage
column) the distinct category terms needed for the mapping, no cell contents
are copied into the profile.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from openpyxl.utils import get_column_letter

from .. import __version__
from ..provenance import detect_embedded_tag
from ..util import normalize_text_key, sha256_file, utc_now
from .mapping import ColumnMapping, SheetMapping, StageMapping, WorkbookMapping
from .reader import SheetData, WorkbookData, read_workbook

HEADER_SCAN_ROWS = 20

#: (role, header keywords). First match wins; a role already used on the sheet is not proposed twice.
ROLE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doi", ("doi",)),
    ("pmid", ("pmid", "pubmed id")),
    ("url", ("url", "link", "web address")),
    ("title", ("title",)),
    ("authors", ("author",)),
    ("year", ("year",)),
    ("journal", ("journal",)),
    ("stage", ("stage",)),
    ("limitations", ("limitation", "caveat", "limits", "concern")),
    ("measurement_method", ("method", "assay", "technique", "instrument")),
    ("timepoint", ("timepoint", "time point")),
    ("outcome", ("outcome", "endpoint")),
    ("comparator", ("comparator", "control", "comparison")),
    ("intervention", ("intervention", "exposure", "treatment", "material")),
    ("system_context", ("system", "population", "setting")),
    ("experimental_context", ("condition", "context", "setup")),
    ("measurements", ("measurement", "measured", "value", "quantity")),
    ("narrative_findings", ("finding", "result", "summary", "observation", "evidence", "key point", "notes")),
    ("paper_reference", ("reference", "citation", "paper", "publication", "study", "source")),
)
_SUMMARY_SHEET_WORDS = ("summary", "overview", "count", "total", "tally")


def _detect_header_row(sd: SheetData) -> tuple[int | None, list[dict]]:
    """Likely header row: the early row with the most text cells. Returns (row, scored candidates)."""
    scored = []
    for r in range(1, min(sd.max_row, HEADER_SCAN_ROWS) + 1):
        cells = [sd.cells[(r, c)] for c in range(1, sd.max_col + 1) if (r, c) in sd.cells
                 and sd.cells[(r, c)].raw_text not in (None, "")]
        if len(cells) < 2:
            continue
        n_text = sum(1 for c in cells if c.value_type == "str" and len(c.raw_text or "") <= 80)
        if n_text / len(cells) < 0.6:
            continue
        below = sum(1 for c in range(1, sd.max_col + 1) if (r + 1, c) in sd.cells or (r + 1, c) in sd.merge_anchor)
        scored.append({"row": r, "text_cells": n_text, "non_empty_cells": len(cells), "cells_in_next_row": below})
    if not scored:
        return None, []
    best = max(scored, key=lambda s: (s["text_cells"], s["cells_in_next_row"] > 0, -s["row"]))
    return best["row"], sorted(scored, key=lambda s: -s["text_cells"])[:3]


def _infer_role(header: str | None, used: set[str]) -> tuple[str, str | None]:
    if not header:
        return "unmapped", None
    key = normalize_text_key(header)
    for role, words in ROLE_KEYWORDS:
        for w in words:
            if w in key:
                if role in used and role not in ("narrative_findings", "measurements", "limitations"):
                    return "unmapped", f"header contains '{w}' but role '{role}' is already proposed on this sheet"
                return role, f"header contains '{w}'"
    return "unmapped", None


def _profile_sheet(sd: SheetData) -> dict:
    header_row, candidates = _detect_header_row(sd)
    data_start = header_row + 1 if header_row else None
    columns = []
    data_rows: set[int] = set()
    fills: dict[str, dict] = {}
    fonts: Counter[str] = Counter()
    n_links = n_comments = n_formulas = 0
    for (r, c), cell in sd.cells.items():
        if cell.hyperlink:
            n_links += 1
        if cell.comment:
            n_comments += 1
        if cell.formula:
            n_formulas += 1
        if cell.fill_color:
            f = fills.setdefault(cell.fill_color, {"cells": 0, "columns": set(), "min_row": r, "max_row": r,
                                                   "header_cells": 0})
            f["cells"] += 1
            f["columns"].add(get_column_letter(c))
            f["min_row"], f["max_row"] = min(f["min_row"], r), max(f["max_row"], r)
            if header_row and r == header_row:
                f["header_cells"] += 1
        if cell.font_flags and not (header_row and r <= header_row):
            for flag in cell.font_flags.split(","):
                fonts[flag] += 1
        if data_start and r >= data_start and cell.raw_text not in (None, ""):
            data_rows.add(r)
    for c in range(1, sd.max_col + 1):
        header = sd.cells[(header_row, c)].raw_text if header_row and (header_row, c) in sd.cells else None
        types: Counter[str] = Counter()
        lengths: list[int] = []
        links = comments = multiline = with_digits = merged = 0
        for r in range(data_start or 1, sd.max_row + 1):
            cell = sd.cells.get((r, c))
            if (r, c) in sd.merge_anchor and (cell is None or not cell.is_merge_anchor):
                merged += 1
            if cell is None:
                continue
            if cell.hyperlink:
                links += 1
            if cell.comment:
                comments += 1
            if cell.raw_text in (None, ""):
                continue
            types[cell.value_type] += 1
            lengths.append(len(cell.raw_text))
            if "\n" in cell.raw_text:
                multiline += 1
            if any(ch.isdigit() for ch in cell.raw_text):
                with_digits += 1
        n = sum(types.values())
        if n == 0 and not header:
            continue
        columns.append({
            "column": get_column_letter(c), "header": header, "non_empty_cells": n,
            "value_types": dict(types), "max_text_length": max(lengths) if lengths else 0,
            "mean_text_length": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "multi_line_cells": multiline, "cells_containing_digits": with_digits,
            "hyperlinks": links, "comments": comments, "cells_covered_by_merges": merged,
        })
    return {
        "sheet": sd.name, "hidden": sd.hidden, "used_rows": sd.max_row, "used_columns": sd.max_col,
        "non_empty_cells": sum(1 for c in sd.cells.values() if c.raw_text not in (None, "")),
        "header_row_detected": header_row, "header_row_candidates": candidates,
        "data_start_row_detected": data_start, "data_rows_with_content": len(data_rows),
        "merged_ranges": len(sd.merged_ranges), "merged_range_examples": sd.merged_ranges[:5],
        "hyperlinks": n_links, "comments": n_comments, "formula_cells": n_formulas,
        "fill_colors": [{"color": k, "cells": v["cells"], "columns": sorted(v["columns"]),
                         "rows": f"{v['min_row']}-{v['max_row']}", "header_cells": v["header_cells"]}
                        for k, v in sorted(fills.items(), key=lambda kv: -kv[1]["cells"])],
        "font_flags_in_data_rows": dict(fonts),
        "columns": columns,
    }


def profile_workbook(path: Path | str, workbook: WorkbookData | None = None) -> dict:
    """Profile a workbook. Reads the file; never writes to it."""
    path = Path(path)
    wb = workbook or read_workbook(path)
    return {
        "profile_version": 1, "generated_at": utc_now(), "geodesic_version": __version__,
        "file_name": path.name, "sha256": sha256_file(path), "size_bytes": path.stat().st_size,
        "embedded_synthetic_tag": detect_embedded_tag(path),
        "sheet_count": len(wb.sheets),
        "sheets": [_profile_sheet(sd) for sd in wb.sheets],
        "notes": [
            "Header rows and column roles are detected heuristically and must be confirmed in the mapping file.",
            "Formatting (fill colours, fonts) is reported as found. GEODESIC does not interpret its meaning.",
        ],
    }


def render_profile_md(profile: dict) -> str:
    """Short human-readable profile."""
    out = [f"# Workbook profile: {profile['file_name']}", "",
           f"- SHA-256: `{profile['sha256']}`", f"- Size: {profile['size_bytes']} bytes",
           f"- Sheets: {profile['sheet_count']}",
           f"- Embedded GEODESIC synthetic tag: {profile['embedded_synthetic_tag'] or 'none'}",
           f"- Generated: {profile['generated_at']} (GEODESIC {profile['geodesic_version']})", ""]
    out += [f"> {n}" for n in profile["notes"]] + [""]
    for s in profile["sheets"]:
        out += [f"## Sheet `{s['sheet']}`" + (" (hidden)" if s["hidden"] else ""), "",
                f"- Used range: {s['used_rows']} rows x {s['used_columns']} columns; "
                f"{s['non_empty_cells']} non-empty cells",
                f"- Header row (detected, unconfirmed): {s['header_row_detected'] or 'none found'}; "
                f"data rows with content below it: {s['data_rows_with_content']}",
                f"- Merged ranges: {s['merged_ranges']}"
                + (f" (e.g. {', '.join(s['merged_range_examples'])})" if s["merged_range_examples"] else ""),
                f"- Hyperlinks: {s['hyperlinks']}; cell comments: {s['comments']}; formula cells: {s['formula_cells']}"]
        if s["fill_colors"]:
            out.append("- Fill colours (meaning not interpreted): " + "; ".join(
                f"{f['color']} on {f['cells']} cells, columns {','.join(f['columns'])}, rows {f['rows']}"
                for f in s["fill_colors"]))
        if s["font_flags_in_data_rows"]:
            out.append("- Font formatting in data rows (meaning not interpreted): " + ", ".join(
                f"{k}: {v}" for k, v in sorted(s["font_flags_in_data_rows"].items())))
        if s["columns"]:
            out += ["", "| Col | Header | Non-empty | Types | Max len | Multi-line | With digits | Links | Comments | Merged |",
                    "|---|---|---|---|---|---|---|---|---|---|"]
            for c in s["columns"]:
                types = ", ".join(f"{k}:{v}" for k, v in sorted(c["value_types"].items())) or "-"
                header = (c["header"] or "").replace("|", "\\|").replace("\n", " ")
                out.append(f"| {c['column']} | {header} | {c['non_empty_cells']} | {types} | {c['max_text_length']} "
                           f"| {c['multi_line_cells']} | {c['cells_containing_digits']} | {c['hyperlinks']} "
                           f"| {c['comments']} | {c['cells_covered_by_merges']} |")
        out.append("")
    return "\n".join(out)


def starter_mapping(profile: dict, workbook: WorkbookData, stages: list[dict] | None = None,
                    workbook_key: str = "main") -> WorkbookMapping:
    """Generate a starter mapping from a profile. Every inferred entry is ``confirmed: false``.

    ``stages`` is the project's stage list (``key`` and ``name``); when given, sheet
    names and stage-column terms are matched against it. Matching is by name only
    and is always left unconfirmed.
    """
    stages = stages or []
    stage_norm = [(s["key"], {normalize_text_key(s["key"]), normalize_text_key(s.get("name") or s["key"])})
                  for s in stages]

    def match_stage(text: str) -> str | None:
        key = normalize_text_key(text)
        hits = [k for k, names in stage_norm if any(n and (n == key or n in key) for n in names)]
        return hits[0] if len(hits) == 1 else None

    sheets: list[SheetMapping] = []
    for sp in profile["sheets"]:
        sd = workbook.sheet(sp["sheet"])
        used: set[str] = set()
        cols: list[ColumnMapping] = []
        for cp in sp["columns"]:
            role, why = _infer_role(cp["header"], used)
            types = cp["value_types"]
            n_numeric = types.get("number", 0) + types.get("formula", 0)
            if role in ("paper_reference", "title", "authors", "journal") and n_numeric * 2 > max(1, sum(types.values())):
                role, why = "unmapped", f"{why}, but most cells in the column are numbers or formulas"
            if role != "unmapped":
                used.add(role)
            cols.append(ColumnMapping(column=cp["column"], header=cp["header"], role=role, confirmed=False,
                                      inferred_by=why))
        has_identity = any(c.role in ("paper_reference", "doi", "pmid", "title") for c in cols)
        name_key = normalize_text_key(sp["sheet"])
        if sp["header_row_detected"] and has_identity:
            role, why = "evidence", "a header row was detected and a column header suggests paper references"
            if any(w in name_key for w in _SUMMARY_SHEET_WORDS):
                why += "; the sheet name also suggests a summary - check which it is"
        elif any(w in name_key for w in _SUMMARY_SHEET_WORDS):
            role, why = "summary", "sheet name suggests a summary; add summary_counts entries if it holds counts"
        else:
            role, why = "ignore", "no header row with a paper-reference column was detected"
        stage = StageMapping(mode="none", confirmed=False, inferred_by="no stage information detected")
        if role == "evidence":
            stage_col = next((c for c in cols if c.role == "stage"), None)
            if stage_col is not None and sd is not None:
                terms: list[str] = []
                for r in range(sp["data_start_row_detected"] or 1, sd.max_row + 1):
                    cell = sd.effective(r, stage_col.index)
                    if cell is not None and cell.raw_text:
                        for term in cell.raw_text.split(";"):
                            if term.strip() and term.strip() not in terms:
                                terms.append(term.strip())
                stage = StageMapping(mode="column", value_map={t: (match_stage(t) or "") for t in terms[:200]},
                                     confirmed=False,
                                     inferred_by=f"column {stage_col.column} header suggests stage names; "
                                                 "terms matched to project stages by name only")
            else:
                hit = match_stage(sp["sheet"])
                if hit:
                    stage = StageMapping(mode="fixed", stage_keys=[hit], confirmed=False,
                                         inferred_by=f"sheet name resembles project stage '{hit}'")
        sheets.append(SheetMapping(
            sheet=sp["sheet"], role=role, confirmed=False, inferred_by=why,
            header_row=sp["header_row_detected"] if role == "evidence" else None,
            data_start_row=sp["data_start_row_detected"] if role == "evidence" else None,
            stage=stage, columns=cols if role == "evidence" else [],
        ))
    return WorkbookMapping(
        workbook_key=workbook_key,
        generated_from={"file_name": profile["file_name"], "sha256": profile["sha256"],
                        "profiled_at": profile["generated_at"]},
        sheets=sheets, summary_counts=[],
    )


def write_profile(workbook_path: Path | str, out_dir: Path | str, stages: list[dict] | None = None,
                  workbook_key: str = "main") -> dict:
    """Profile a workbook and write ``profile.md``, ``profile.json`` and ``mapping.starter.yaml`` into ``out_dir``.

    The workbook is opened read-only and never written. Every inferred entry of the starter mapping is
    marked unconfirmed. Returns ``{"profile", "profile_md", "profile_json", "mapping"}`` (the last three are paths).
    """
    import json

    from .mapping import write_mapping

    src = Path(workbook_path).expanduser()
    out = Path(out_dir).expanduser()
    wb = read_workbook(src)
    profile = profile_workbook(src, wb)
    mapping = starter_mapping(profile, wb, stages or [], workbook_key)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"profile_md": out / "profile.md", "profile_json": out / "profile.json",
             "mapping": out / "mapping.starter.yaml"}
    paths["profile_md"].write_text(render_profile_md(profile), encoding="utf-8")
    paths["profile_json"].write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    write_mapping(mapping, paths["mapping"])
    return {"profile": profile, **paths}
