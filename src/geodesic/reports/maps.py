"""Evidence maps: how many findings a project holds for each stage and term.

An evidence map is a cross-tabulation of the project's stages against the
terms of one descriptive field (system, outcome ...). It shows where evidence
has been recorded and where none has; it says nothing about what the evidence
shows, and its counts are counts of *records in this project*.

Counting rules
--------------
* A finding assigned to several stages is counted in each of them, so stage
  rows do not add up to the total. Totals are counted over distinct findings.
* Terms are the original terms as stored. Two terms share a column only when a
  reviewer mapped both to one normalized term (:mod:`geodesic.vocabulary`);
  spelling variants are never merged silently.
* ``independent_units_at_most`` uses :func:`geodesic.comparison.independence_units`:
  findings of one experiment, and findings a reviewer related as dependent,
  form one unit. Where independence was not assessed each finding is its own
  unit, so the figure is an upper bound.
* Only the ``max_columns`` most frequent columns are shown individually; the
  rest are added up in an explicitly labelled "other" column.

The CLI, the dashboard, the figures and the HTML report all call
:func:`evidence_map`.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import comparison, query, vocabulary
from ..errors import ValidationError
from ..export.tables import Table

MAP_FIELDS = vocabulary.FIELDS
METRICS = ("findings", "reports", "independent_units_at_most", "verified", "quantitative", "quantitative_candidates")
METRIC_LABELS = {
    "findings": "findings",
    "reports": "reports (papers) the findings come from",
    "independent_units_at_most": "independent units, at most",
    "verified": "findings verified by a reviewer",
    "quantitative": "findings stating a number, range or bound",
    "quantitative_candidates": "findings a reviewer marked as quantitative-comparison candidates",
}
NO_STAGE = "(no stage)"
NOT_RECORDED = "(not recorded)"
FIELD_TITLES = {"system_context": "system", "experimental_context": "experimental context", "outcome": "outcome",
                "measurement_method": "measurement method", "intervention": "intervention",
                "comparator": "comparator"}


def _metrics(items: list[tuple], unit_of: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Counts over distinct findings. ``items`` are ``(finding_id, report_id, verified, quantitative, candidate)``."""
    seen: dict[str, tuple] = {}
    for it in items:
        seen[it[0]] = it
    return {
        "findings": len(seen),
        "reports": len({it[1] for it in seen.values()}),
        "independent_units_at_most": len({unit_of[fid]["unit"] if fid in unit_of else f"fnd:{fid}" for fid in seen}),
        "verified": sum(1 for it in seen.values() if it[2]),
        "quantitative": sum(1 for it in seen.values() if it[3]),
        "quantitative_candidates": sum(1 for it in seen.values() if it[4]),
    }


def evidence_map(conn: sqlite3.Connection, by: str = "system_context", *,
                 flt: query.FindingFilter | None = None, max_columns: int = 12) -> dict[str, Any]:
    """Stage × term cross-tabulation of active findings (see the module docstring for the counting rules)."""
    if by not in MAP_FIELDS:
        raise ValidationError(f"An evidence map can be drawn by one of {', '.join(MAP_FIELDS)}; got {by!r}")
    if max_columns < 1:
        raise ValidationError("max_columns must be at least 1")
    w = query._finding_where(flt or query.FindingFilter())
    rows = conn.execute(
        f"SELECT f.id, f.{by} AS term, COALESCE(p.canonical_id, p.id) AS report, f.review_status, f.statement_type,"  # noqa: S608
        " f.comparison_eligibility FROM finding f JOIN paper p ON p.id = f.paper_id" + w.sql() + " ORDER BY f.pk",
        w.params).fetchall()
    stage_rows = conn.execute("SELECT id, key, name, active FROM stage ORDER BY position").fetchall()
    stages_of: dict[str, list[str]] = {}
    for r in conn.execute("SELECT fs.finding_id, s.key FROM finding_stage fs JOIN stage s ON s.id = fs.stage_id"
                          " WHERE fs.status = 'active'"):
        stages_of.setdefault(r[0], []).append(r[1])
    mapped = vocabulary.mapping(conn).get(by, {})
    unit_of = comparison.independence_units(conn)

    # column of every finding
    col_items: dict[str, list[tuple]] = {}
    col_terms: dict[str, set[str]] = {}
    col_kind: dict[str, str] = {}
    by_row_col: dict[tuple[str, str], list[tuple]] = {}
    by_row: dict[str, list[tuple]] = {}
    everything: list[tuple] = []
    multi_stage = 0
    for r in rows:
        term = r["term"]
        if term in (None, ""):
            col, kind = NOT_RECORDED, "not_recorded"
        else:
            col, from_mapping = vocabulary.group_label(mapped, term)
            kind = "mapped" if from_mapping else "term"
            col_terms.setdefault(col, set()).add(term)
        if col_kind.get(col, kind) != kind:
            kind = "mapped"                       # an original term equal to another term's normalized label
        col_kind[col] = kind
        item = (r["id"], r["report"], r["review_status"] == "verified",
                r["statement_type"] in query.QUANTITATIVE_TYPES, r["comparison_eligibility"] == "quantitative_candidate")
        keys = stages_of.get(r["id"]) or [NO_STAGE]
        multi_stage += len(keys) > 1
        col_items.setdefault(col, []).append(item)
        everything.append(item)
        for k in keys:
            by_row.setdefault(k, []).append(item)
            by_row_col.setdefault((k, col), []).append(item)

    # most frequent columns first; "(not recorded)" always last among the shown columns
    named = sorted((c for c in col_items if c != NOT_RECORDED), key=lambda c: (-len(col_items[c]), c))
    shown, folded = named[:max_columns], named[max_columns:]
    other_label = f"other ({len(folded)} term{'s' if len(folded) != 1 else ''})"
    columns: list[dict[str, Any]] = []
    for c in shown:
        columns.append({"key": c, "label": c, "kind": col_kind[c], "original_terms": sorted(col_terms.get(c, ())),
                        **_metrics(col_items[c], unit_of)})
    if folded:
        items = [it for c in folded for it in col_items[c]]
        columns.append({"key": other_label, "label": other_label, "kind": "other",
                        "original_terms": sorted(t for c in folded for t in col_terms.get(c, ())),
                        **_metrics(items, unit_of)})
    if NOT_RECORDED in col_items:
        columns.append({"key": NOT_RECORDED, "label": NOT_RECORDED, "kind": "not_recorded", "original_terms": [],
                        **_metrics(col_items[NOT_RECORDED], unit_of)})
    col_of = {c: (other_label if c in folded else c) for c in col_items}

    out_rows: list[dict[str, Any]] = []
    for s in stage_rows:
        if not s["active"] and s["key"] not in by_row:
            continue
        out_rows.append({"key": s["key"], "label": s["name"], "kind": "stage",
                         **_metrics(by_row.get(s["key"], []), unit_of)})
    if NO_STAGE in by_row:
        out_rows.append({"key": NO_STAGE, "label": NO_STAGE, "kind": "no_stage",
                         **_metrics(by_row[NO_STAGE], unit_of)})
    cells: dict[str, dict[str, dict[str, int]]] = {}
    merged: dict[tuple[str, str], list[tuple]] = {}
    for (rk, c), items in by_row_col.items():
        merged.setdefault((rk, col_of[c]), []).extend(items)
    for (rk, ck), items in merged.items():
        cells.setdefault(rk, {})[ck] = _metrics(items, unit_of)

    notes = [
        "Counts are counts of findings recorded in this project. An empty cell means that no finding is "
        "recorded there; it does not mean that no evidence exists.",
        "A finding assigned to several stages is counted in each of them"
        + (f" ({multi_stage} finding(s) here)" if multi_stage else "") + "; totals count each finding once.",
        "Columns are the terms as stored. Terms share a column only where a reviewer mapped them to one "
        "normalized term; such columns list their original terms.",
        "'Independent units, at most' counts findings of one experiment, and findings a reviewer related as "
        "dependent, once. Independence of the remaining findings was not assessed, so the figure is an upper bound.",
    ]
    if folded:
        notes.append(f"The {len(folded)} least frequent terms are added up in the column '{other_label}'.")
    return {"by": by, "title": f"Evidence map: stage by {FIELD_TITLES[by]}", "field_title": FIELD_TITLES[by],
            "metrics": list(METRICS), "rows": out_rows, "columns": columns, "cells": cells,
            "total": _metrics(everything, unit_of), "findings_in_several_stages": multi_stage, "notes": notes}


def cell(emap: dict[str, Any], row_key: str, column_key: str, metric: str = "findings") -> int:
    """Value of one cell (0 when no finding is recorded there)."""
    return emap["cells"].get(row_key, {}).get(column_key, {}).get(metric, 0)


def map_table(emap: dict[str, Any]) -> Table:
    """The map in long form (one row per non-empty cell) for the table exports."""
    name = f"evidence_map_{emap['by']}"
    cols = ["stage_key", "stage", "column", "column_kind", "original_terms", *METRICS]
    labels = {r["key"]: r["label"] for r in emap["rows"]}
    rows = []
    for r in emap["rows"]:
        for c in emap["columns"]:
            m = emap["cells"].get(r["key"], {}).get(c["key"])
            if m:
                rows.append({"stage_key": r["key"], "stage": labels[r["key"]], "column": c["label"],
                             "column_kind": c["kind"], "original_terms": "; ".join(c["original_terms"]), **m})
    return Table(name, emap["title"], cols, rows, " ".join(emap["notes"]))


def format_map(emap: dict[str, Any], metric: str = "findings", width: int = 16) -> str:
    """Plain-text grid of one metric."""
    if metric not in METRICS:
        raise ValidationError(f"metric must be one of {', '.join(METRICS)}")

    def clip(text: str, n: int) -> str:
        return text if len(text) <= n else text[: n - 1] + "…"

    first = max([len(r["label"]) for r in emap["rows"]] + [10])
    first = min(first, 28)
    head = " " * first + " | " + " | ".join(clip(c["label"], width).rjust(width) for c in emap["columns"]) \
        + " | " + "all terms".rjust(10)
    lines = [f"{emap['title']} — {METRIC_LABELS[metric]}", "", head, "-" * len(head)]
    for r in emap["rows"]:
        vals = [cell(emap, r["key"], c["key"], metric) for c in emap["columns"]]
        lines.append(clip(r["label"], first).ljust(first) + " | "
                     + " | ".join((str(v) if v else "·").rjust(width) for v in vals) + " | " + str(r[metric]).rjust(10))
    lines.append("-" * len(head))
    lines.append("all stages".ljust(first) + " | " + " | ".join(str(c[metric]).rjust(width) for c in emap["columns"])
                 + " | " + str(emap["total"][metric]).rjust(10))
    mapped = [c for c in emap["columns"] if c["kind"] == "mapped"]
    for c in mapped:
        lines.append(f"  '{c['label']}' groups (reviewer mapping): " + "; ".join(c["original_terms"]))
    lines += [""] + [f"Note: {n}" for n in emap["notes"]]
    return "\n".join(lines)
